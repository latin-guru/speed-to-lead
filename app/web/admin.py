"""Admin-only dashboard pages: dead-letter jobs, the send kill switch and settings, users,
and the audit log. Every route needs an admin permission, checked on the server."""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from app.api.deps import SessionDep
from app.config import Settings
from app.followups.guards import KILL_SWITCH_FLAG, set_sends_paused
from app.jobs import kinds
from app.jobs.queue import release_deferred, retry_dead
from app.models import Job, JobStatus, SystemFlag, User, UserRole, utcnow
from app.security.authz import Permission, Principal
from app.services import audit, mfa
from app.services.users import UserError, create_user, set_password, unlock, update_user
from app.web.common import render, require, require_post

router = APIRouter(prefix="/dashboard/admin", tags=["dashboard"], include_in_schema=False)

AUDIT_PAGE = 50
USER_ERRORS = {
    "demo": "User changes are turned off in the public demo.",
    "not_found": "That user does not exist.",
    "invalid_email": "Enter a valid email address.",
    "invalid_role": "Pick admin, rep or viewer.",
    # pragma: allowlist nextline secret
    "weak_password": "Passwords need 12 to 128 characters, not the email.",
    "duplicate": "A user with that email already exists.",
    "self_change": "You cannot change your own role or deactivate yourself.",
    "last_admin": "Keep at least one active admin.",
    "self_reset": "Reset your own two-factor from your account page instead.",
}
USER_DONE = {
    "created": "User created.",
    "updated": "User updated. Their open sessions were ended.",
    "unlocked": "Account unlocked.",
    "password": "Password reset. Their open sessions were ended.",  # pragma: allowlist secret
    "mfa_reset": "Two-factor reset. They set it up again at their next login.",
}


def _job_row(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "last_error": job.last_error,
        "lead_id": job.lead_id,
        "updated_at": job.updated_at,
    }


# --------------------------------------------------------------------------- jobs


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(
    request: Request,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require(Permission.RETRY_JOBS))],
    done: int | None = None,
) -> HTMLResponse:
    dead = session.scalars(
        select(Job)
        .where(Job.status == JobStatus.DEAD.value)
        .order_by(Job.updated_at.desc())
        .limit(200)
    )
    return render(
        request,
        "admin_jobs.html",
        {"page": "admin", "section": "jobs", "jobs": [_job_row(j) for j in dead], "done": done},
    )


@router.post("/jobs/{job_id}/retry")
def job_retry(
    job_id: int,
    request: Request,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require_post(Permission.RETRY_JOBS))],
) -> Response:
    job = session.get(Job, job_id)
    retried = job is not None and retry_dead(session, job_id, now=utcnow())
    audit.record_for(
        session,
        principal,
        "job_retried",
        request=request,
        target_type="job",
        target_id=job_id,
        outcome="ok" if retried else "failed",
        summary={"kind": job.kind if job is not None else None},
    )
    session.commit()
    return RedirectResponse(f"/dashboard/admin/jobs?done={job_id if retried else 0}", 303)


# --------------------------------------------------------------------------- system


def _settings_rows(settings: Settings, crm_name: str) -> list[tuple[str, str]]:
    """Non-secret settings worth seeing at a glance. Secrets are only shown as set/unset."""

    def is_set(value: object) -> str:
        return "set" if value else "not set"

    return [
        ("Environment", settings.environment),
        ("AI provider", settings.ai_provider),
        ("CRM adapter", crm_name),
        ("Business timezone", settings.business_timezone),
        ("Sending window", settings.send_windows if settings.quiet_hours_enabled else "off"),
        (
            "Touches per week / gap",
            f"{settings.max_touches_per_week} / {settings.min_touch_gap_hours}h",
        ),
        ("Phone cap per day", str(settings.phone_daily_cap)),
        (
            "Session length / idle",
            f"{settings.session_ttl_hours}h / {settings.session_idle_minutes}m",
        ),
        ("Lockout after", f"{settings.login_max_failures} failed logins"),
        ("Two-factor for admins", "required" if settings.admin_2fa_required else "optional"),
        ("Retention", f"{settings.retention_days} days ({settings.retention_mode})"),
        ("CORS origins", ", ".join(settings.cors_origins) or "none (CORS off)"),
        ("ADMIN_TOKEN (machine API)", is_set(settings.admin_token)),
        (
            "Field encryption key",
            "development key" if settings.uses_dev_field_keys else "set",
        ),
    ]


@router.get("/system", response_class=HTMLResponse)
def system_page(
    request: Request,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require(Permission.VIEW_SETTINGS))],
    released: int | None = None,
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    flag = session.get(SystemFlag, KILL_SWITCH_FLAG)
    db_paused = flag is not None and flag.value == "true"
    crm = request.app.state.job_context.crm
    return render(
        request,
        "admin_system.html",
        {
            "page": "admin",
            "section": "system",
            "env_paused": settings.sends_paused,
            "db_paused": db_paused,
            "flag": flag,
            "released": released,
            "rows": _settings_rows(settings, crm.name if crm is not None else "none"),
        },
    )


@router.post("/kill-switch")
async def kill_switch(
    request: Request,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require_post(Permission.KILL_SWITCH))],
) -> Response:
    form = await request.form()
    paused = form.get("paused") == "1"
    now = utcnow()
    set_sends_paused(session, paused, actor=principal.actor[:48], now=now)
    released = 0
    if not paused:
        released = release_deferred(session, kinds.FOLLOWUP_SEND, "kill_switch", now=now)
    audit.record_for(
        session,
        principal,
        "kill_switch",
        request=request,
        target_type="system",
        target_id="sends_paused",
        summary={"paused": paused, "released_jobs": released},
    )
    session.commit()
    return RedirectResponse(f"/dashboard/admin/system?released={released}", status_code=303)


# --------------------------------------------------------------------------- users


@router.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require(Permission.MANAGE_USERS))],
    error: str | None = None,
    done: str | None = None,
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    users = list(session.scalars(select(User).order_by(User.email)))
    return render(
        request,
        "admin_users.html",
        {
            "page": "admin",
            "section": "users",
            "users": users,
            "roles": [r.value for r in UserRole],
            "now": utcnow(),
            "error": USER_ERRORS.get(error or ""),
            "done": USER_DONE.get(done or ""),
            "locked_writes": settings.demo_mode,
        },
    )


def _user_redirect(error: UserError | str | None = None, done: str | None = None) -> Response:
    query: dict[str, str] = {}
    if error:
        query["error"] = error.code if isinstance(error, UserError) else error
    if done:
        query["done"] = done
    suffix = f"?{urlencode(query)}" if query else ""
    return RedirectResponse(f"/dashboard/admin/users{suffix}", status_code=303)


def _demo_locked(request: Request) -> bool:
    settings: Settings = request.app.state.settings
    return settings.demo_mode


@router.post("/users")
async def user_create(
    request: Request,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require_post(Permission.MANAGE_USERS))],
) -> Response:
    if _demo_locked(request):
        return _user_redirect("demo")
    form = await request.form()
    try:
        user = create_user(
            session,
            str(form.get("email") or "")[:254],
            str(form.get("password") or "")[:1024],
            str(form.get("role") or ""),
        )
    except UserError as exc:
        session.rollback()
        return _user_redirect(exc)
    audit.record_for(
        session,
        principal,
        "user_created",
        request=request,
        target_type="user",
        target_id=user.id,
        summary={"email": user.email, "role": user.role},
    )
    session.commit()
    return _user_redirect(done="created")


def _target(session: SessionDep, user_id: int) -> User | None:
    return session.get(User, user_id)


@router.post("/users/{user_id}")
async def user_update(
    user_id: int,
    request: Request,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require_post(Permission.MANAGE_USERS))],
) -> Response:
    if _demo_locked(request):
        return _user_redirect("demo")
    user = _target(session, user_id)
    if user is None:
        return _user_redirect("not_found")
    form = await request.form()
    try:
        changes = update_user(
            session,
            user,
            acting_user_id=principal.user_id,
            role=str(form.get("role") or "") or None,
            active=form.get("active") == "1",
            now=utcnow(),
        )
    except UserError as exc:
        session.rollback()
        return _user_redirect(exc)
    if changes:
        audit.record_for(
            session,
            principal,
            "user_updated",
            request=request,
            target_type="user",
            target_id=user.id,
            summary={"email": user.email, "changes": changes},
        )
    session.commit()
    return _user_redirect(done="updated")


@router.post("/users/{user_id}/unlock")
def user_unlock(
    user_id: int,
    request: Request,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require_post(Permission.MANAGE_USERS))],
) -> Response:
    user = _target(session, user_id)
    if user is None:
        return _user_redirect("not_found")
    unlock(user)
    audit.record_for(
        session,
        principal,
        "user_unlocked",
        request=request,
        target_type="user",
        target_id=user.id,
        summary={"email": user.email},
    )
    session.commit()
    return _user_redirect(done="unlocked")


@router.post("/users/{user_id}/password")
async def user_password(
    user_id: int,
    request: Request,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require_post(Permission.MANAGE_USERS))],
) -> Response:
    if _demo_locked(request):
        return _user_redirect("demo")
    user = _target(session, user_id)
    if user is None:
        return _user_redirect("not_found")
    form = await request.form()
    try:
        set_password(session, user, str(form.get("password") or "")[:1024], now=utcnow())
    except UserError as exc:
        session.rollback()
        return _user_redirect(exc)
    audit.record_for(
        session,
        principal,
        "password_reset",
        request=request,
        target_type="user",
        target_id=user.id,
        summary={"email": user.email},
    )
    session.commit()
    return _user_redirect(done="password")


@router.post("/users/{user_id}/2fa-reset")
def user_mfa_reset(
    user_id: int,
    request: Request,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require_post(Permission.MANAGE_USERS))],
) -> Response:
    """For a lost phone: clears the user's two-factor and recovery codes, ends sessions."""
    if _demo_locked(request):
        return _user_redirect("demo")
    user = _target(session, user_id)
    if user is None:
        return _user_redirect("not_found")
    if user.id == principal.user_id:
        return _user_redirect("self_reset")
    mfa.disable(session, user, now=utcnow())
    audit.record_for(
        session,
        principal,
        "mfa_reset",
        request=request,
        target_type="user",
        target_id=user.id,
        summary={"email": user.email},
    )
    session.commit()
    return _user_redirect(done="mfa_reset")


# --------------------------------------------------------------------------- audit log


@router.get("/audit", response_class=HTMLResponse)
def audit_page(
    request: Request,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require(Permission.VIEW_AUDIT))],
    action: str | None = None,
    actor: str | None = None,
    target: str | None = None,
    outcome: str | None = None,
    page: int = Query(default=1, ge=1, le=10_000),
) -> HTMLResponse:
    actions = audit.known_actions(session)
    filters = {
        "action": action if action in actions else None,
        "actor": (actor or "").strip()[:254] or None,
        "target_id": (target or "").strip()[:64] or None,
        "outcome": outcome if outcome in ("ok", "denied", "failed") else None,
    }
    events, total = audit.list_events(
        session, limit=AUDIT_PAGE, offset=(page - 1) * AUDIT_PAGE, **filters
    )
    query = {
        k: v
        for k, v in (
            ("action", filters["action"]),
            ("actor", filters["actor"]),
            ("target", filters["target_id"]),
            ("outcome", filters["outcome"]),
        )
        if v
    }
    pages = max(1, (total + AUDIT_PAGE - 1) // AUDIT_PAGE)
    return render(
        request,
        "admin_audit.html",
        {
            "page": "admin",
            "section": "audit",
            "events": events,
            "total": total,
            "actions": actions,
            "filters": filters,
            "page_no": page,
            "pages": pages,
            "prev_url": f"?{urlencode({**query, 'page': page - 1})}" if page > 1 else None,
            "next_url": f"?{urlencode({**query, 'page': page + 1})}" if page < pages else None,
        },
    )

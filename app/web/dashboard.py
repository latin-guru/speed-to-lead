"""Server-rendered operator dashboard at ``/dashboard`` (Jinja + a little vanilla JS).

People log in with their own account (``app.web.auth``); what they can do depends on their
role (``app.security.authz``). Contact details are always masked on lists and on the lead
page. Revealing them is a POST that needs the reveal permission on that lead (a rep: only
their own leads), and it is written to the lead's timeline and the audit log.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import SessionDep
from app.api.metrics import build_kpis
from app.config import Settings
from app.followups.engine import ResumeError, resume_sequence
from app.logging import mask_pii
from app.models import Job, Lead, LeadStage, OutboxMessage, Task, TaskStatus, User, UserRole, utcnow
from app.security.authz import Permission, Principal, can_on_lead, has
from app.services import audit
from app.services import metrics as m
from app.services.display import display_actor
from app.services.pipeline import ALLOWED_TRANSITIONS, TransitionError, move_stage
from app.services.timeline import add_activity
from app.web.common import Forbidden, render, require, require_post

log = logging.getLogger(__name__)
router = APIRouter(tags=["dashboard"], include_in_schema=False)

PAGE_SIZE = 25
STAGE_ERRORS = {
    "invalid_transition": "That stage change is not allowed from the current stage.",
    "same_stage": "The lead is already in that stage.",
    "reason_required": "A reason is required for that change.",
    "human_only": "Only a person can change a stage.",
}
NOTICES = {
    "assigned": "Assignment saved.",
    "task_closed": "Task closed.",
}
TASK_ERRORS = {
    "other_open_tasks": "Close the lead's other open tasks before resuming follow-ups.",
    "opted_out": "The lead opted out, so follow-ups cannot resume. The task was not closed.",
    "stage_final": "The lead is contacted or closed, so follow-ups stay stopped.",
    "not_paused": "Follow-ups are not paused for this lead.",
    "already_done": "That task is already closed.",
}

Viewer = Annotated[Principal, Depends(require(Permission.VIEW_DASHBOARD))]
LeadViewer = Annotated[Principal, Depends(require(Permission.VIEW_LEADS))]


# --------------------------------------------------------------------------- pages


@router.get("/dashboard", response_class=HTMLResponse)
def overview(request: Request, session: SessionDep, principal: Viewer) -> HTMLResponse:
    kpis = build_kpis(session, utcnow())  # first paint; the page then polls the JSON API
    return render(request, "dashboard.html", {"kpis": kpis, "page": "overview"})


def _int_or_none(value: str | None, low: int, high: int) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return max(low, min(high, int(value)))
    except ValueError:
        return None


@router.get("/dashboard/leads", response_class=HTMLResponse)
def lead_list(
    request: Request,
    session: SessionDep,
    principal: LeadViewer,
    stage: str | None = None,
    source: str | None = None,
    min_score: str | None = None,
    max_score: str | None = None,
    assigned: Literal["me", "none", ""] = "",
    page: int = Query(default=1, ge=1, le=10_000),
) -> HTMLResponse:
    stage = stage if stage in {s.value for s in LeadStage} else None
    sources = m.known_sources(session)
    source = source if source in sources else None
    if assigned == "me" and principal.user_id is None:
        assigned = ""
    filters = m.LeadFilter(
        stage=stage,
        source=source,
        min_score=_int_or_none(min_score, 0, 100),
        max_score=_int_or_none(max_score, 0, 100),
        assignee_id=principal.user_id if assigned == "me" else None,
        unassigned=assigned == "none",
    )
    leads, total = m.list_leads(session, filters, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    query: dict[str, Any] = {
        k: v
        for k, v in (
            ("stage", filters.stage),
            ("source", filters.source),
            ("min_score", filters.min_score),
            ("max_score", filters.max_score),
            ("assigned", assigned or None),
        )
        if v is not None
    }
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return render(
        request,
        "leads.html",
        {
            "page": "leads",
            "leads": [_lead_row(lead) for lead in leads],
            "total": total,
            "filters": filters,
            "assigned": assigned,
            "stages": [s.value for s in LeadStage],
            "sources": sources,
            "page_no": page,
            "pages": pages,
            "prev_url": f"?{urlencode({**query, 'page': page - 1})}" if page > 1 else None,
            "next_url": f"?{urlencode({**query, 'page': page + 1})}" if page < pages else None,
        },
    )


def _response_seconds(lead: Lead) -> float | None:
    if lead.first_response_at is None:
        return None
    return (lead.first_response_at - lead.created_at).total_seconds()


def _lead_row(lead: Lead) -> dict[str, Any]:
    return {
        "id": lead.id,
        "name": lead.name,
        "source": lead.source,
        "stage": lead.stage,
        "score": lead.score,
        "next_step": lead.next_step,
        "created_at": lead.created_at,
        "response_seconds": _response_seconds(lead),
        "opted_out": lead.opted_out_at is not None,
        "assignee": lead.assignee.actor_name if lead.assignee is not None else None,
    }


def _mask_contact(value: str | None) -> str:
    return mask_pii(value) if value else ""


def _get_lead(session: Session, lead_id: uuid.UUID) -> Lead:
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    return lead


def _assignable(session: Session) -> list[User]:
    return list(
        session.scalars(
            select(User)
            .where(User.active.is_(True), User.role != UserRole.VIEWER.value)
            .order_by(User.email)
        )
    )


def _detail(
    request: Request,
    session: Session,
    lead: Lead,
    principal: Principal,
    *,
    revealed: bool = False,
    notice: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    outbox = list(
        session.scalars(
            select(OutboxMessage).where(OutboxMessage.lead_id == lead.id).order_by(OutboxMessage.id)
        )
    )
    tasks = list(session.scalars(select(Task).where(Task.lead_id == lead.id).order_by(Task.id)))
    crm_job = session.scalars(
        select(Job).where(Job.lead_id == lead.id, Job.kind == "crm_sync").order_by(Job.id.desc())
    ).first()
    show = (lambda v: v or "") if revealed else _mask_contact
    timeline = [
        {
            "at": a.created_at,
            "kind": a.kind,
            "actor": display_actor(a.actor),
            "summary": m.activity_summary(a.kind, a.detail),
        }
        for a in sorted(lead.activities, key=lambda a: (a.created_at, a.id), reverse=True)
    ]
    mine = lead.assignee_id is not None and lead.assignee_id == principal.user_id
    settings: Settings = request.app.state.settings
    return render(
        request,
        "lead_detail.html",
        {
            "page": "leads",
            "lead": lead,
            "phone": show(lead.phone),
            "email": show(lead.email),
            "message": (lead.message or "") if revealed else mask_pii(lead.message or ""),
            "revealed": revealed,
            "response_seconds": _response_seconds(lead),
            "timeline": timeline,
            "outbox": [
                {
                    "sent_at": o.sent_at,
                    "step": o.step,
                    "channel": o.channel,
                    "to": show(o.to_address),
                    "subject": o.subject,
                    "body": o.body if revealed else mask_pii(o.body),
                    "sender": o.sender,
                }
                for o in outbox
            ],
            "tasks": tasks,
            "crm_job": crm_job,
            "crm_adapter": _crm_label(request),
            "targets": sorted(s.value for s in ALLOWED_TRANSITIONS[LeadStage(lead.stage)]),
            "assignee": lead.assignee.actor_name if lead.assignee is not None else None,
            "mine": mine,
            "may": {
                "reveal": can_on_lead(principal, Permission.REVEAL_CONTACT, lead),
                "stage": can_on_lead(principal, Permission.CHANGE_STAGE, lead),
                "tasks": can_on_lead(principal, Permission.CLOSE_TASK, lead),
                "assign_self": can_on_lead(principal, Permission.ASSIGN_SELF, lead)
                and principal.user_id is not None
                and not mine,
                "assign_any": has(principal, Permission.ASSIGN_ANY),
                "reveal_needs_assign": has(principal, Permission.REVEAL_CONTACT)
                and not can_on_lead(principal, Permission.REVEAL_CONTACT, lead),
            },
            "assignable": _assignable(session) if has(principal, Permission.ASSIGN_ANY) else [],
            "notice": notice,
            "error": error,
            "retention_days": settings.retention_days,
        },
        status_code=status_code,
    )


def _crm_label(request: Request) -> str:
    crm = request.app.state.job_context.crm
    return crm.name if crm is not None else "none (CRM_PROVIDER=none)"


@router.get("/dashboard/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(
    lead_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    principal: LeadViewer,
    moved: str | None = None,
    done: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    lead = _get_lead(session, lead_id)
    notice = NOTICES.get(done or "")
    if moved in {s.value for s in LeadStage}:
        notice = f"Moved to {moved}."
    return _detail(
        request,
        session,
        lead,
        principal,
        notice=notice,
        error=STAGE_ERRORS.get(error or "") or TASK_ERRORS.get(error or ""),
    )


def _deny(
    session: Session, request: Request, principal: Principal, action: str, lead: Lead
) -> Forbidden:
    """Log a refused action on a lead (useful when someone probes other reps' leads)."""
    audit.record_for(
        session,
        principal,
        action,
        request=request,
        target_type="lead",
        target_id=lead.id,
        outcome="denied",
    )
    session.commit()
    return Forbidden("That lead is not assigned to you.")


@router.post("/dashboard/leads/{lead_id}/reveal", response_class=HTMLResponse)
def reveal_contact(
    lead_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require_post(Permission.REVEAL_CONTACT))],
) -> HTMLResponse:
    lead = _get_lead(session, lead_id)
    if not can_on_lead(principal, Permission.REVEAL_CONTACT, lead):
        raise _deny(session, request, principal, "contact_revealed", lead)
    add_activity(session, lead, "contact_revealed", principal.actor, {"fields": ["phone", "email"]})
    audit.record_for(
        session,
        principal,
        "contact_revealed",
        request=request,
        target_type="lead",
        target_id=lead.id,
        summary={"fields": ["phone", "email"]},
    )
    session.commit()
    session.refresh(lead)
    return _detail(request, session, lead, principal, revealed=True)


@router.post("/dashboard/leads/{lead_id}/stage")
async def lead_stage_submit(
    lead_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require_post(Permission.CHANGE_STAGE))],
) -> Response:
    form = await request.form()
    lead = _get_lead(session, lead_id)
    if not can_on_lead(principal, Permission.CHANGE_STAGE, lead):
        raise _deny(session, request, principal, "stage_changed", lead)
    settings: Settings = request.app.state.settings
    base = f"/dashboard/leads/{lead.id}"
    try:
        target = LeadStage(str(form.get("stage") or ""))
    except ValueError:
        return RedirectResponse(f"{base}?error=invalid_transition", status_code=303)
    before = lead.stage
    try:
        move_stage(
            session,
            lead,
            target,
            actor=principal.actor,
            reason=str(form.get("reason") or "")[:200],
            now=utcnow(),
            max_attempts=settings.job_max_attempts,
            crm_enabled=settings.crm_provider != "none",
        )
    except TransitionError as exc:
        session.rollback()
        return RedirectResponse(f"{base}?error={exc.code}", status_code=303)
    audit.record_for(
        session,
        principal,
        "stage_changed",
        request=request,
        target_type="lead",
        target_id=lead.id,
        summary={"from": before, "to": target.value},
    )
    session.commit()
    return RedirectResponse(f"{base}?moved={target.value}", status_code=303)


@router.post("/dashboard/leads/{lead_id}/assign")
async def lead_assign(
    lead_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require_post(Permission.ASSIGN_SELF))],
) -> Response:
    """``assignee=me`` takes an unassigned lead. Admins can pick anyone, or nobody."""
    form = await request.form()
    lead = _get_lead(session, lead_id)
    choice = str(form.get("assignee") or "")
    target: User | None
    if choice == "me":
        if principal.user_id is None or not can_on_lead(principal, Permission.ASSIGN_SELF, lead):
            raise _deny(session, request, principal, "lead_assigned", lead)
        target = session.get(User, principal.user_id)
    else:
        if not has(principal, Permission.ASSIGN_ANY):
            raise _deny(session, request, principal, "lead_assigned", lead)
        target = None
        if choice:
            target = session.get(User, int(choice)) if choice.isdigit() else None
            if target is None or not target.active or target.role == UserRole.VIEWER.value:
                raise HTTPException(status_code=422, detail="pick an active admin or rep")
    before = lead.assignee.actor_name if lead.assignee is not None else None
    lead.assignee_id = target.id if target is not None else None
    after = target.actor_name if target is not None else None
    add_activity(session, lead, "lead_assigned", principal.actor, {"from": before, "to": after})
    audit.record_for(
        session,
        principal,
        "lead_assigned",
        request=request,
        target_type="lead",
        target_id=lead.id,
        summary={"from": before, "to": after},
    )
    session.commit()
    return RedirectResponse(f"/dashboard/leads/{lead.id}?done=assigned", status_code=303)


@router.post("/dashboard/tasks/{task_id}/close")
async def task_close(
    task_id: int,
    request: Request,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require_post(Permission.CLOSE_TASK))],
) -> Response:
    form = await request.form()
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    lead = _get_lead(session, task.lead_id)
    base = f"/dashboard/leads/{lead.id}"
    if not can_on_lead(principal, Permission.CLOSE_TASK, lead):
        raise _deny(session, request, principal, "task_closed", lead)
    if task.status == TaskStatus.DONE.value:
        return RedirectResponse(f"{base}?error=already_done", status_code=303)
    now = utcnow()
    resume = form.get("resume_sequence") == "1"
    task.status = TaskStatus.DONE.value
    task.done_at = now
    task.assignee = task.assignee or principal.name[:64]
    resumed = None
    if resume:
        others = session.scalar(
            select(Task.id).where(
                Task.lead_id == lead.id, Task.id != task.id, Task.status == TaskStatus.OPEN.value
            )
        )
        try:
            if others is not None:
                raise ResumeError("other_open_tasks", "close the lead's other tasks first")
            resumed = resume_sequence(session, lead, actor=principal.actor, now=now)
        except ResumeError as exc:
            session.rollback()
            return RedirectResponse(f"{base}?error={exc.code}", status_code=303)
    add_activity(
        session,
        lead,
        "task_closed",
        principal.actor,
        {"task_id": task.id, "reason": task.reason, "resumed_sends": resumed},
    )
    audit.record_for(
        session,
        principal,
        "task_closed",
        request=request,
        target_type="task",
        target_id=task.id,
        summary={"lead_id": str(lead.id), "reason": task.reason, "resumed_sends": resumed},
    )
    session.commit()
    return RedirectResponse(f"{base}?done=task_closed", status_code=303)

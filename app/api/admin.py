"""Operator endpoints: the dead-letter queue and the send kill switch."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select

from app.api.deps import AdminDep, SessionDep, TokenPrincipal
from app.config import Settings
from app.followups.guards import KILL_SWITCH_FLAG, set_sends_paused
from app.jobs import kinds
from app.jobs.queue import release_deferred, retry_dead
from app.models import Job, SystemFlag, utcnow
from app.schemas import JobOut, KillSwitchIn, KillSwitchOut
from app.services import audit

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[AdminDep])


def _job_out(job: Job) -> JobOut:
    return JobOut(
        id=job.id,
        kind=job.kind,
        status=job.status,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        run_at=job.run_at,
        idempotency_key=job.idempotency_key,
        last_error=job.last_error,
        lead_id=job.lead_id,
        updated_at=job.updated_at,
    )


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(
    session: SessionDep,
    status: Literal["queued", "running", "done", "dead", "cancelled"] = "dead",
    limit: int = Query(default=100, ge=1, le=500),
) -> list[JobOut]:
    stmt = select(Job).where(Job.status == status).order_by(Job.updated_at.desc()).limit(limit)
    return [_job_out(j) for j in session.scalars(stmt)]


@router.post("/jobs/{job_id}/retry", response_model=JobOut)
def retry_job(
    job_id: int, request: Request, session: SessionDep, principal: TokenPrincipal
) -> JobOut:
    if not retry_dead(session, job_id, now=utcnow()):
        if session.get(Job, job_id) is None:
            raise HTTPException(status_code=404, detail="job not found")
        raise HTTPException(status_code=409, detail="only dead jobs can be retried")
    audit.record_for(
        session, principal, "job_retried", request=request, target_type="job", target_id=job_id
    )
    session.commit()
    job = session.get(Job, job_id, populate_existing=True)
    assert job is not None
    return _job_out(job)


def _kill_switch(session: SessionDep, settings: Settings, released: int = 0) -> KillSwitchOut:
    flag = session.get(SystemFlag, KILL_SWITCH_FLAG)
    db_paused = flag is not None and flag.value == "true"
    return KillSwitchOut(
        paused=settings.sends_paused or db_paused,
        env_paused=settings.sends_paused,
        db_paused=db_paused,
        released_jobs=released,
    )


@router.get("/kill-switch", response_model=KillSwitchOut)
def get_kill_switch(request: Request, session: SessionDep) -> KillSwitchOut:
    return _kill_switch(session, request.app.state.settings)


@router.post("/kill-switch", response_model=KillSwitchOut)
def set_kill_switch(
    body: KillSwitchIn, request: Request, session: SessionDep, principal: TokenPrincipal
) -> KillSwitchOut:
    """Pause or resume every automated send. Paused jobs wait; none are dropped."""
    now = utcnow()
    set_sends_paused(session, body.paused, actor=f"human:{body.actor}", now=now)
    released = 0
    if not body.paused:  # resume: run what the switch held back now (guards still apply)
        released = release_deferred(session, kinds.FOLLOWUP_SEND, "kill_switch", now=now)
    audit.record_for(
        session,
        principal,
        "kill_switch",
        request=request,
        target_type="system",
        target_id="sends_paused",
        summary={"paused": body.paused, "released_jobs": released, "as": body.actor},
    )
    session.commit()
    return _kill_switch(session, request.app.state.settings, released)

"""Handoff tasks for humans."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select

from app.api.deps import AdminDep, SessionDep, TokenPrincipal
from app.followups.engine import ResumeError, resume_sequence
from app.models import Lead, Task, TaskStatus, utcnow
from app.schemas import TaskOut, TaskUpdateIn
from app.services import audit
from app.services.pipeline import HUMAN_PREFIX

router = APIRouter(prefix="/api/tasks", tags=["tasks"], dependencies=[AdminDep])


def _out(task: Task, resumed: int | None = None) -> TaskOut:
    return TaskOut(
        id=task.id,
        lead_id=task.lead_id,
        reason=task.reason,
        detail=task.detail,
        assignee=task.assignee,
        status=task.status,
        created_at=task.created_at,
        done_at=task.done_at,
        resumed_sends=resumed,
    )


@router.get("", response_model=list[TaskOut])
def list_tasks(
    session: SessionDep,
    status: Literal["open", "done", "all"] = "open",
    limit: int = Query(default=100, ge=1, le=500),
) -> list[TaskOut]:
    stmt = select(Task).order_by(Task.created_at, Task.id).limit(limit)
    if status != "all":
        stmt = stmt.where(Task.status == status)
    return [_out(t) for t in session.scalars(stmt)]


@router.patch("/{task_id}", response_model=TaskOut)
def update_task(
    task_id: int,
    body: TaskUpdateIn,
    request: Request,
    session: SessionDep,
    principal: TokenPrincipal,
) -> TaskOut:
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if body.assignee is not None:
        task.assignee = body.assignee
    now = utcnow()
    if body.status is not None and body.status != task.status:
        task.status = body.status
        task.done_at = now if body.status == TaskStatus.DONE.value else None
    resumed: int | None = None
    if body.resume_sequence:
        assert body.actor is not None  # TaskUpdateIn guarantees it
        resumed = _resume(session, task, HUMAN_PREFIX + body.actor, now)
    if body.status == TaskStatus.DONE.value or body.assignee is not None:
        audit.record_for(
            session,
            principal,
            "task_closed" if body.status == TaskStatus.DONE.value else "task_updated",
            request=request,
            target_type="task",
            target_id=task.id,
            summary={"assignee": body.assignee, "status": body.status, "resumed_sends": resumed},
        )
    session.commit()
    return _out(task, resumed)


def _resume(session: SessionDep, task: Task, actor: str, now: datetime) -> int:
    """Close-and-resume: only when no other handoff task is still open for the lead."""
    others = session.scalar(
        select(Task.id).where(
            Task.lead_id == task.lead_id, Task.id != task.id, Task.status == TaskStatus.OPEN.value
        )
    )
    if others is not None:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": "other_open_tasks", "message": "close the lead's other tasks first"},
        )
    lead = session.get(Lead, task.lead_id)
    assert lead is not None  # FK
    try:
        return resume_sequence(session, lead, actor=actor, now=now)
    except ResumeError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409, detail={"code": exc.code, "message": str(exc)}
        ) from exc

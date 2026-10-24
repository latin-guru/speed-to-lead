"""Handoff tasks for humans."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.api.deps import AdminDep, SessionDep
from app.models import Task, TaskStatus, utcnow
from app.schemas import TaskOut, TaskUpdateIn

router = APIRouter(prefix="/api/tasks", tags=["tasks"], dependencies=[AdminDep])


def _out(task: Task) -> TaskOut:
    return TaskOut(
        id=task.id,
        lead_id=task.lead_id,
        reason=task.reason,
        detail=task.detail,
        assignee=task.assignee,
        status=task.status,
        created_at=task.created_at,
        done_at=task.done_at,
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
def update_task(task_id: int, body: TaskUpdateIn, session: SessionDep) -> TaskOut:
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if body.assignee is not None:
        task.assignee = body.assignee
    if body.status is not None and body.status != task.status:
        task.status = body.status
        task.done_at = utcnow() if body.status == TaskStatus.DONE.value else None
    session.commit()
    return _out(task)

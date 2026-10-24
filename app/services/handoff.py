"""Human handoff: pause automation and put the lead in front of a person."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.followups.engine import pause_sequence
from app.models import Lead, Task, TaskStatus
from app.services.timeline import add_activity

log = logging.getLogger(__name__)

# Why a lead was handed off. Kept short: it is stored and shown in the task list.
LEAD_REPLIED = "lead_replied"
AI_HUMAN_REVIEW = "ai_human_review"
LOW_CONFIDENCE = "low_confidence"


def handoff(
    session: Session, lead: Lead, reason: str, *, now: datetime, detail: str | None = None
) -> Task:
    """Pause the sequence, open a task (one open task per lead and reason), log an activity.

    The caller commits.
    """
    cancelled = pause_sequence(session, lead, f"handoff:{reason}", now=now)
    task = session.scalar(
        select(Task).where(
            Task.lead_id == lead.id, Task.reason == reason, Task.status == TaskStatus.OPEN.value
        )
    )
    created = task is None
    if task is None:
        task = Task(lead_id=lead.id, reason=reason, detail=(detail or "")[:300] or None)
        task.created_at = now
        session.add(task)
        session.flush()
    add_activity(
        session,
        lead,
        "handoff",
        "system",
        {"reason": reason, "task_id": task.id, "new_task": created, "cancelled_sends": cancelled},
    )
    log.info("handoff", extra={"lead_id": str(lead.id), "reason": reason, "task_id": task.id})
    return task

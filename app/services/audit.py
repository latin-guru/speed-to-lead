"""The audit log: who did what, when, from where (as a keyed hash), with what result.

Events are written in the same transaction as the action they describe, so an action and
its audit record are saved together or not at all. Summaries are scrubbed: strings pass
through the same masking as the logs, values are length-capped, and callers never put
contact details in them.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from typing import Any

from fastapi import Request
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.logging import mask_pii
from app.models import AuditEvent, utcnow
from app.security.authz import Principal

MAX_SUMMARY_KEYS = 20
MAX_VALUE_CHARS = 200


def ip_hash(key: bytes, ip: str | None) -> str | None:
    if not ip:
        return None
    return hmac.new(key, f"audit-ip|{ip}".encode(), hashlib.sha256).hexdigest()[:24]


def _clean(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return mask_pii(value)[:MAX_VALUE_CHARS]
    if isinstance(value, bool | int | float) or value is None:
        return value
    if depth >= 3:
        return mask_pii(str(value))[:MAX_VALUE_CHARS]
    if isinstance(value, dict):
        items = list(value.items())[:MAX_SUMMARY_KEYS]
        return {str(k)[:48]: _clean(v, depth + 1) for k, v in items}
    if isinstance(value, list | tuple | set):
        return [_clean(v, depth + 1) for v in list(value)[:MAX_SUMMARY_KEYS]]
    return mask_pii(str(value))[:MAX_VALUE_CHARS]


def record(
    session: Session,
    *,
    actor: str,
    role: str | None,
    action: str,
    target_type: str | None = None,
    target_id: object = None,
    outcome: str = "ok",
    summary: dict[str, Any] | None = None,
    request: Request | None = None,
    now: datetime | None = None,
) -> AuditEvent:
    """Add an audit event to ``session`` (the caller commits)."""
    ip = ua = None
    if request is not None:
        from app.api.deps import client_ip

        ip = ip_hash(request.app.state.settings.secret_key_bytes, client_ip(request))
        ua = (request.headers.get("user-agent") or "")[:200] or None
    event = AuditEvent(
        at=now or utcnow(),
        actor=actor[:254],
        role=role,
        action=action[:48],
        outcome=outcome,
        target_type=target_type,
        target_id=None if target_id is None else str(target_id)[:64],
        summary=_clean(summary or {}),
        ip_hash=ip,
        user_agent=ua,
    )
    session.add(event)
    return event


def record_for(
    session: Session,
    principal: Principal,
    action: str,
    *,
    request: Request | None = None,
    target_type: str | None = None,
    target_id: object = None,
    outcome: str = "ok",
    summary: dict[str, Any] | None = None,
) -> AuditEvent:
    return record(
        session,
        actor=principal.audit_actor,
        role=principal.role.value,
        action=action,
        target_type=target_type,
        target_id=target_id,
        outcome=outcome,
        summary=summary,
        request=request,
    )


def query(
    *,
    action: str | None = None,
    actor: str | None = None,
    target_id: str | None = None,
    outcome: str | None = None,
) -> Select[AuditEvent]:
    stmt = select(AuditEvent)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    if actor:
        stmt = stmt.where(AuditEvent.actor == actor)
    if target_id:
        stmt = stmt.where(AuditEvent.target_id == target_id)
    if outcome:
        stmt = stmt.where(AuditEvent.outcome == outcome)
    return stmt


def list_events(
    session: Session, *, limit: int, offset: int, **filters: str | None
) -> tuple[list[AuditEvent], int]:
    stmt = query(**filters)
    total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = session.scalars(
        stmt.order_by(AuditEvent.at.desc(), AuditEvent.id.desc()).limit(limit).offset(offset)
    )
    return list(rows), total


def known_actions(session: Session) -> list[str]:
    return sorted(session.scalars(select(AuditEvent.action).distinct()))

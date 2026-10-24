"""Small time phrases for after-hours messages ("last night", "this morning")."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def received_phrase(created: datetime, now: datetime, zone: ZoneInfo) -> str:
    """When the lead reached us, relative to ``now``, in the lead's local time."""
    sent_local, created_local = now.astimezone(zone), created.astimezone(zone)
    days = (sent_local.date() - created_local.date()).days
    if days <= 0:
        return "last night" if created_local.hour < 5 else "earlier today"
    if days == 1:
        return "last night" if created_local.hour >= 17 else "yesterday"
    return f"on {created_local.strftime('%A')}"


def call_phrase(now: datetime, zone: ZoneInfo) -> str:
    hour = now.astimezone(zone).hour
    if hour < 12:
        return "this morning"
    if hour < 17:
        return "this afternoon"
    return "this evening"

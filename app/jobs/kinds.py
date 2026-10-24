"""Job kinds and their idempotency keys (one place, so producers and tests agree)."""

from __future__ import annotations

import uuid

SCORE_LEAD = "score_lead"
FOLLOWUP_SEND = "followup_send"
CRM_SYNC = "crm_sync"

ALL_KINDS = (SCORE_LEAD, FOLLOWUP_SEND, CRM_SYNC)


def score_key(lead_id: uuid.UUID) -> str:
    return f"{SCORE_LEAD}:{lead_id}"


def followup_key(lead_id: uuid.UUID, step: str) -> str:
    return f"{FOLLOWUP_SEND}:{lead_id}:{step}"


def send_key(lead_id: uuid.UUID, step: str) -> str:
    """Outbox key: a given step goes to a given lead at most once, ever."""
    return f"{lead_id}:{step}"


def crm_key(lead_id: uuid.UUID, stage: str, revision: int = 0) -> str:
    """One sync per stage *per reopen*: a reopened lead can reach the same stage again.

    ``revision`` is how many times the lead was reopened (0 for most leads, which keeps
    their keys unchanged).
    """
    base = f"{CRM_SYNC}:{lead_id}:{stage}"
    return f"{base}:r{revision}" if revision else base

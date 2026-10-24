"""Parse Meta Lead Ads-style ``leadgen`` webhook payloads.

Shape handled::

    {"object": "page",
     "entry": [{"id": "<page_id>", "time": 1700000000,
                "changes": [{"field": "leadgen",
                             "value": {"leadgen_id": "...", "form_id": "...", "page_id": "...",
                                       "ad_id": "...", "created_time": 1700000000,
                                       "field_data": [{"name": "full_name",
                                                       "values": ["..."]}]}}]}]}

Note: production Meta webhooks send only ids; the answers are fetched from the Graph API by
``leadgen_id``. ``field_data`` inline is supported here (test tools / sandbox shape); changes
without it are recorded as ``needs_fetch`` for a later fetcher (see README open decisions).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.services.normalize import MESSAGE_MAX

_ID_RE = re.compile(r"^[0-9A-Za-z_\-]{1,64}$")
_NAME_FIELDS = {"full_name", "name"}
_EMAIL_FIELDS = {"email", "email_address", "work_email"}
_PHONE_FIELDS = {"phone_number", "phone", "mobile_phone"}


class PayloadError(ValueError):
    pass


@dataclass(frozen=True)
class LeadgenChange:
    leadgen_id: str
    form_id: str | None
    page_id: str | None
    ad_id: str | None
    fields: dict[str, str] | None = field(default=None)


def _clean_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if _ID_RE.match(text) else None


def _field_map(field_data: Any) -> dict[str, str]:
    if not isinstance(field_data, list):
        raise PayloadError("field_data must be a list")
    out: dict[str, str] = {}
    for item in field_data[:50]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip().lower()[:64]
        values = item.get("values")
        if not name or not isinstance(values, list):
            continue
        out[name] = ", ".join(str(v) for v in values[:10])[:MESSAGE_MAX]
    return out


def parse_payload(payload: Any) -> list[LeadgenChange]:
    if not isinstance(payload, dict) or payload.get("object") != "page":
        raise PayloadError("expected object=page")
    entries = payload.get("entry")
    if not isinstance(entries, list):
        raise PayloadError("entry must be a list")
    changes: list[LeadgenChange] = []
    for entry in entries[:100]:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict) or change.get("field") != "leadgen":
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            leadgen_id = _clean_id(value.get("leadgen_id"))
            if leadgen_id is None:
                continue
            fields = _field_map(value["field_data"]) if "field_data" in value else None
            changes.append(
                LeadgenChange(
                    leadgen_id=leadgen_id,
                    form_id=_clean_id(value.get("form_id")),
                    page_id=_clean_id(value.get("page_id") or entry.get("id")),
                    ad_id=_clean_id(value.get("ad_id")),
                    fields=fields,
                )
            )
    return changes


def lead_fields(fields: dict[str, str]) -> dict[str, Any]:
    """Map Meta form answers to ``LeadIn`` keys. Custom questions become the message."""
    name = next((fields[k] for k in _NAME_FIELDS if k in fields), "")
    if not name:
        name = " ".join(p for p in (fields.get("first_name"), fields.get("last_name")) if p)
    email = next((fields[k] for k in _EMAIL_FIELDS if k in fields), None)
    phone = next((fields[k] for k in _PHONE_FIELDS if k in fields), None)
    known = _NAME_FIELDS | _EMAIL_FIELDS | _PHONE_FIELDS | {"first_name", "last_name"}
    extra = [
        f"{k.replace('_', ' ').rstrip('?').strip().capitalize()}: {v}"
        for k, v in fields.items()
        if k not in known and v
    ]
    message = "\n".join(extra)[:MESSAGE_MAX] or None
    return {"name": name, "email": email, "phone": phone, "message": message, "source": "meta"}

"""Structured JSON logging with a PII-masking filter.

The filter is attached to the *handler*, not a logger, so every record that reaches the
output is scrubbed, including records propagated from third-party loggers.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

# Phone-like sequences: optional +1, then 3-3-4 digits with common separators.
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.\-]?)?\(?(\d{3})\)?[\s.\-]?(\d{3})[\s.\-]?(\d{4})(?!\d)")
_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]*@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")

# Attributes every LogRecord has; anything else came in through `extra=`.
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


def mask_pii(text: str) -> str:
    """Mask phone numbers (keep last 4) and emails (keep first char and domain)."""
    text = _EMAIL_RE.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", text)
    return _PHONE_RE.sub(lambda m: f"***-***-{m.group(3)}", text)


def _mask_value(value: Any) -> Any:
    if isinstance(value, str):
        return mask_pii(value)
    if isinstance(value, dict):
        return {k: _mask_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_mask_value(v) for v in value]
    return value


class PIIMaskingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask_pii(record.getMessage())
        record.args = None
        for key, value in list(record.__dict__.items()):
            if key not in _RESERVED:
                setattr(record, key, _mask_value(value))
        return True


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = mask_pii(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(PIIMaskingFilter())
    handler.setFormatter(
        JSONFormatter() if json_output else logging.Formatter("%(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, "_speed_to_lead", False):
            root.removeHandler(existing)
    handler._speed_to_lead = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level.upper())

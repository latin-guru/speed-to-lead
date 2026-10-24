import json
import logging

import pytest

from app.logging import JSONFormatter, PIIMaskingFilter, configure_logging, mask_pii


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("call +12025550123 now", "call ***-***-0123 now"),
        ("(202) 555-0199", "***-***-0199"),
        ("202.555.0100", "***-***-0100"),
        ("mail jordan@example.com", "mail j***@example.com"),
        ("order 12345 shipped", "order 12345 shipped"),
    ],
)
def test_mask_pii(raw: str, expected: str) -> None:
    assert mask_pii(raw) == expected


def _record(msg: str, *args: object, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, None)
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def test_filter_masks_args_and_extras() -> None:
    record = _record(
        "lead %s", "+12025550123", contact={"email": "ana@example.com", "list": ["202-555-0111"]}
    )
    PIIMaskingFilter().filter(record)
    assert record.getMessage() == "lead ***-***-0123"
    assert record.contact == {"email": "a***@example.com", "list": ["***-***-0111"]}


def test_json_formatter_emits_extras() -> None:
    record = _record("hello", lead_id="abc")
    PIIMaskingFilter().filter(record)
    out = json.loads(JSONFormatter().format(record))
    assert out["msg"] == "hello"
    assert out["lead_id"] == "abc"
    assert out["level"] == "INFO"


def test_configured_handler_masks_child_logger_output(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", json_output=True)
    logging.getLogger("some.library").info("user typed jordan@example.com and 2025550123")
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert "jordan@example.com" not in line
    assert "2025550123" not in line
    assert "j***@example.com" in line
    assert "***-***-0123" in line


def test_exception_text_is_masked(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", json_output=True)
    try:
        raise ValueError("bad phone 202-555-0177")
    except ValueError:
        logging.getLogger("x").exception("boom")
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert "202-555-0177" not in line

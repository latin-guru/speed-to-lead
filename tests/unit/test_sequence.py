from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.followups.sequence import Sequence, first_name, load_sequence, render


def _seq(**step: Any) -> dict[str, Any]:
    base = {"id": "s1", "channel": "sms", "delay_minutes": 0, "body": "Hi {first_name}"}
    base.update(step)
    return {"name": "test_seq", "steps": [base]}


def test_default_sequence_loads() -> None:
    seq = load_sequence()
    assert [s.id for s in seq.steps] == ["ack_sms", "ack_email", "reminder_1d", "nudge_3d"]
    assert seq.step("ack_sms") is not None
    assert seq.step("ack_sms").transactional
    assert seq.step("nope") is None
    for step in seq.steps:
        assert "{" not in render(step.body, name="Jo Lee", company="Acme", callback_minutes=5)


def test_custom_sequence_file(tmp_path: Path) -> None:
    path = tmp_path / "seq.yaml"
    path.write_text(
        "name: tiny\nsteps:\n  - {id: only, channel: sms, delay_minutes: 5, body: hi}\n"
    )
    assert load_sequence(path).steps[0].delay_minutes == 5


@pytest.mark.parametrize(
    "step",
    [
        {"body": "Hi {phone}"},  # unknown placeholder
        {"body": "Hi {first_name!r}"},  # conversions not allowed
        {"body": "Hi {first_name:>10}"},  # format specs not allowed
        {"body": "Hi {0}"},  # positional not allowed
        {"channel": "email"},  # email needs a subject
        {"subject": "nope"},  # sms cannot have one
        {"channel": "fax"},
        {"delay_minutes": -1},
        {"id": "Bad Id"},
        {"extra": 1},
    ],
)
def test_invalid_steps_rejected(step: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Sequence.model_validate(_seq(**step))


def test_duplicate_step_ids_rejected() -> None:
    raw = _seq()
    raw["steps"].append(dict(raw["steps"][0]))
    with pytest.raises(ValidationError, match="unique"):
        Sequence.model_validate(raw)


def test_render_treats_lead_name_as_data() -> None:
    out = render(
        "Hi {first_name} from {company}", name="{company} {x}", company="Acme", callback_minutes=1
    )
    assert out == "Hi {company} from Acme"
    assert first_name("   ") == "there"

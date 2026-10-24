import pytest

from app.ai.base import InvalidOutputError
from app.ai.parsing import parse_score_output
from tests.unit.ai_helpers import model_json


def test_plain_json() -> None:
    assert parse_score_output(model_json()).score == 82


@pytest.mark.parametrize(
    "wrapped",
    [
        "```json\n{body}\n```",
        "Here is the assessment:\n{body}\nThanks!",
        "{trailing}",
    ],
)
def test_one_repair_pass(wrapped: str) -> None:
    body = model_json()
    trailing = body[:-1] + ",}"
    text = wrapped.replace("{body}", body).replace("{trailing}", trailing)
    assert parse_score_output(text).next_step.value == "call_now"


@pytest.mark.parametrize(
    "text",
    [
        "no json here",
        "{ definitely not json }",
        "[1, 2, 3]",
        '{"score": 50}',  # valid JSON, invalid schema
        model_json(next_step="wire_money"),
    ],
)
def test_invalid_output(text: str) -> None:
    with pytest.raises(InvalidOutputError):
        parse_score_output(text)

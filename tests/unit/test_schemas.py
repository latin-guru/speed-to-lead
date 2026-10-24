import pytest
from pydantic import ValidationError

from app.schemas import LeadIn
from tests.conftest import valid_lead


def test_valid_lead_is_normalized() -> None:
    lead = LeadIn.model_validate(valid_lead(email=" Jordan.Alvarez@Example.com "))
    assert lead.phone == "+12025550123"
    assert lead.email == "jordan.alvarez@example.com"
    assert lead.source == "api"


def test_phone_or_email_required() -> None:
    with pytest.raises(ValidationError, match="at least one of phone or email"):
        LeadIn.model_validate({"name": "Jordan Alvarez"})


def test_email_only_is_enough() -> None:
    lead = LeadIn.model_validate(valid_lead(phone=None))
    assert lead.phone is None


def test_blank_strings_become_none() -> None:
    lead = LeadIn.model_validate(valid_lead(phone="  ", message="   "))
    assert lead.phone is None
    assert lead.message is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"email": "not-an-email"},
        {"phone": "12"},
        {"name": "http://spam.example.com"},
        {"message": "x" * 1001},
        {"source": "Bad Source!"},
        {"unexpected": "field"},
    ],
)
def test_invalid_fields(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        LeadIn.model_validate(valid_lead(**overrides))

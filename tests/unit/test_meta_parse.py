from typing import Any

import pytest

from app.schemas import LeadIn
from app.services.meta_leadgen import PayloadError, lead_fields, parse_payload


def test_parse_fixture(meta_payload: dict[str, Any]) -> None:
    [change] = parse_payload(meta_payload)
    assert change.leadgen_id == "900000000000001"
    assert change.form_id == "700000000000001"
    assert change.ad_id == "600000000000001"
    assert change.fields is not None
    mapped = lead_fields(change.fields)
    lead = LeadIn.model_validate(mapped)
    assert lead.name == "Dana Whitfield"
    assert lead.phone == "+12025550147"
    assert lead.source == "meta_ads"
    assert lead.message == "What do you need help with: Roof leak after the storm\nHome owner: Yes"


def test_first_and_last_name_fallback() -> None:
    mapped = lead_fields({"first_name": "Ana", "last_name": "Reyes", "email": "a@example.com"})
    assert mapped["name"] == "Ana Reyes"
    assert mapped["message"] is None


def test_change_without_field_data(meta_payload: dict[str, Any]) -> None:
    del meta_payload["entry"][0]["changes"][0]["value"]["field_data"]
    [change] = parse_payload(meta_payload)
    assert change.fields is None


def test_ignores_non_leadgen_and_bad_ids(meta_payload: dict[str, Any]) -> None:
    entry = meta_payload["entry"][0]
    entry["changes"].append({"field": "feed", "value": {"leadgen_id": "1"}})
    entry["changes"].append({"field": "leadgen", "value": {"leadgen_id": "bad id; drop"}})
    entry["changes"].append({"field": "leadgen", "value": "nope"})
    assert len(parse_payload(meta_payload)) == 1


@pytest.mark.parametrize("payload", [[], {"object": "user"}, {"object": "page", "entry": {}}])
def test_rejects_wrong_shape(payload: object) -> None:
    with pytest.raises(PayloadError):
        parse_payload(payload)


def test_field_data_must_be_list(meta_payload: dict[str, Any]) -> None:
    meta_payload["entry"][0]["changes"][0]["value"]["field_data"] = {"name": "x"}
    with pytest.raises(PayloadError):
        parse_payload(meta_payload)

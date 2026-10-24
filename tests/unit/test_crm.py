import json
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.crm.base import CRMError, NotConfigured
from app.crm.hubspot import BASE_URL, HubSpotAdapter, property_setup_request
from app.crm.mapping import DEAL_STAGE, contact_properties, deal_properties, split_name
from app.crm.mock import MockHubSpotAdapter
from app.crm.sync import build_crm_adapter, run_crm_sync
from app.db import init_db, make_engine, make_session_factory
from app.models import Lead, LeadStage
from tests.conftest import make_settings

T0 = datetime(2026, 3, 2, 15, tzinfo=UTC)


def _lead(**kw: object) -> Lead:
    base: dict[str, object] = {
        "name": "Maya de la Cruz",
        "email": "maya@example.com",
        "phone": "+12025550133",
        "source": "web",
        "stage": "new",
        "created_at": T0,
    }
    base.update(kw)
    return Lead(**base)


def test_contact_and_deal_mapping() -> None:
    lead = _lead(score=77)
    c = contact_properties(lead)
    assert c["firstname"] == "Maya"
    assert c["lastname"] == "de la Cruz"
    assert c["email"] == "maya@example.com"
    assert c["phone"] == "+12025550133"
    assert c["lifecyclestage"] == "lead"
    assert c["hs_lead_status"] == "NEW"
    d = deal_properties(lead)
    assert d["dealstage"] == "appointmentscheduled"
    assert d["pipeline"] == "default"
    assert d["lead_score"] == "77"
    assert "closed_lost_reason" not in d


def test_every_stage_maps_and_lost_carries_reason() -> None:
    assert set(DEAL_STAGE) == set(LeadStage)
    lost = _lead(stage="lost", lost_reason="too expensive", email=None, phone=None)
    assert deal_properties(lost)["dealstage"] == "closedlost"
    assert deal_properties(lost)["closed_lost_reason"] == "too expensive"
    assert "email" not in contact_properties(lost)
    assert "phone" not in contact_properties(lost)
    assert contact_properties(_lead(stage="won"))["lifecyclestage"] == "customer"
    assert split_name("") == ("", "")


def test_mock_upserts_are_idempotent() -> None:
    crm = MockHubSpotAdapter()
    cid = crm.upsert_contact({"email": "a@example.com", "firstname": "A"}, external_id=None)
    assert crm.upsert_contact({"email": "a@example.com"}, external_id=None) == cid  # by email
    assert crm.upsert_contact({"firstname": "B"}, external_id=cid) == cid
    did = crm.upsert_deal(
        {"dealstage": "x", "external_lead_id": "L1"}, contact_id=cid, external_id=None
    )
    # A retry that lost the saved deal id still finds the deal by external_lead_id.
    assert (
        crm.upsert_deal(
            {"dealstage": "y", "external_lead_id": "L1"}, contact_id=cid, external_id=None
        )
        == did
    )
    assert len(crm.contacts) == 1
    assert len(crm.deals) == 1
    assert crm.deals[did].properties["dealstage"] == "y"
    assert crm.deals[did].associations == {cid}
    assert ("POST", "/crm/v3/objects/deals/batch/upsert") in crm.calls
    # Mock memory lost (worker restart): updating by a known id recreates it, no new id.
    fresh = MockHubSpotAdapter()
    assert fresh.upsert_contact({"firstname": "A"}, external_id=cid) == cid
    assert fresh.upsert_deal({"external_lead_id": "L1"}, contact_id=cid, external_id=did) == did
    with pytest.raises(CRMError, match="external_lead_id"):
        crm.upsert_deal({"dealstage": "x"}, contact_id=cid, external_id=None)


def test_mock_enforces_unique_external_lead_id() -> None:
    crm = MockHubSpotAdapter()
    crm.create_deal({"external_lead_id": "L1"})
    with pytest.raises(CRMError, match="409"):
        crm.create_deal({"external_lead_id": "L1"})  # a naive create cannot duplicate it
    assert len(crm.deals) == 1


def test_retried_sync_job_does_not_duplicate_the_deal(session: Session) -> None:
    """The deal upsert succeeds, then the job fails before commit: the saved ids roll back."""
    lead = _lead()
    session.add(lead)
    session.commit()
    crm = MockHubSpotAdapter()
    run_crm_sync(session, lead.id, crm, now=T0)
    session.rollback()  # e.g. the worker crashed before committing
    assert lead.crm_deal_id is None
    run_crm_sync(session, lead.id, crm, now=T0)  # the retry
    session.commit()
    assert len(crm.deals) == 1
    assert len(crm.contacts) == 1
    assert lead.crm_deal_id in crm.deals


@pytest.fixture
def session() -> Iterator[Session]:
    engine = make_engine("sqlite://")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s
    engine.dispose()


def test_run_crm_sync_twice_updates_same_records(session: Session) -> None:
    lead = _lead()
    session.add(lead)
    session.flush()
    crm = MockHubSpotAdapter()
    assert run_crm_sync(session, lead.id, crm, now=T0) == "synced"
    lead.stage = "qualified"
    assert run_crm_sync(session, lead.id, crm, now=T0) == "synced"
    assert len(crm.contacts) == 1
    assert len(crm.deals) == 1
    deal = crm.deals[lead.crm_deal_id or ""]
    assert deal.properties["dealstage"] == "qualifiedtobuy"
    assert lead.crm_contact_id in crm.contacts
    assert lead.crm_synced_at == T0
    import uuid

    assert run_crm_sync(session, uuid.uuid4(), crm, now=T0) == "skipped:lead_missing"


def test_factory() -> None:
    assert isinstance(build_crm_adapter(make_settings()), MockHubSpotAdapter)
    assert build_crm_adapter(make_settings(crm_provider="none")) is None
    with pytest.raises(NotConfigured):
        build_crm_adapter(make_settings(crm_provider="hubspot"))
    real = build_crm_adapter(
        make_settings(crm_provider="hubspot", hubspot_token=SecretStr("pat-test"))
    )
    assert isinstance(real, HubSpotAdapter)


def test_hubspot_skeleton_refuses_without_token() -> None:
    adapter = HubSpotAdapter(None)
    with pytest.raises(NotConfigured):
        adapter.upsert_contact({"email": "a@example.com"}, external_id=None)
    with pytest.raises(NotConfigured):
        HubSpotAdapter("pat-test").upsert_contact({"email": "a@example.com"}, external_id=None)


def test_hubspot_request_shapes() -> None:
    by_email = HubSpotAdapter.contact_request({"email": "a@example.com"}, None)
    assert by_email == (
        "POST",
        "/crm/v3/objects/contacts/batch/upsert",
        {
            "inputs": [
                {
                    "idProperty": "email",
                    "id": "a@example.com",
                    "properties": {"email": "a@example.com"},
                }
            ]
        },
    )
    phone_only = HubSpotAdapter.contact_request({"external_lead_id": "L1"}, None)
    assert phone_only[2]["inputs"][0]["idProperty"] == "external_lead_id"
    assert HubSpotAdapter.contact_request({}, "9")[:2] == ("PATCH", "/crm/v3/objects/contacts/9")
    method, path, body = HubSpotAdapter.deal_request({"external_lead_id": "L1", "dealstage": "x"})
    assert (method, path) == ("POST", "/crm/v3/objects/deals/batch/upsert")
    assert body["inputs"][0]["idProperty"] == "external_lead_id"
    assert body["inputs"][0]["id"] == "L1"
    with pytest.raises(CRMError, match="required"):
        HubSpotAdapter.deal_request({"dealstage": "x"})
    setup = property_setup_request("deals")
    assert setup[:2] == ("POST", "/crm/v3/properties/deals")
    assert setup[2]["hasUniqueValue"] is True
    assert property_setup_request("contacts")[2]["groupName"] == "contactinformation"


@respx.mock
def test_hubspot_wire_format() -> None:
    contacts = respx.post(f"{BASE_URL}/crm/v3/objects/contacts/batch/upsert").respond(
        200, json={"results": [{"id": "11"}]}
    )
    deals = respx.post(f"{BASE_URL}/crm/v3/objects/deals/batch/upsert").respond(
        200, json={"results": [{"id": "22"}]}
    )
    assoc = respx.put(f"{BASE_URL}/crm/v4/objects/deal/22/associations/default/contact/11").respond(
        200, json={}
    )
    respx.patch(f"{BASE_URL}/crm/v3/objects/contacts/11").respond(200, content=b"")
    props = {"dealstage": "x", "external_lead_id": "L1"}
    with httpx.Client() as client:
        adapter = HubSpotAdapter("pat-test", client=client)
        assert adapter.upsert_contact({"email": "a@example.com"}, external_id=None) == "11"
        assert adapter.upsert_contact({"firstname": "A"}, external_id="11") == "11"
        assert adapter.upsert_deal(props, contact_id="11", external_id=None) == "22"
        assert adapter.upsert_deal(props, contact_id="11", external_id="22") == "22"
    req = contacts.calls.last.request
    assert req.headers["authorization"] == "Bearer pat-test"
    assert json.loads(req.content)["inputs"][0]["idProperty"] == "email"
    assert deals.call_count == 2  # same upsert both times: HubSpot keeps one deal
    assert json.loads(deals.calls.last.request.content)["inputs"][0]["id"] == "L1"
    assert assoc.call_count == 2  # the v4 default association PUT is idempotent


@respx.mock
def test_hubspot_errors_become_crm_errors() -> None:
    respx.post(f"{BASE_URL}/crm/v3/objects/contacts/batch/upsert").respond(429)
    respx.post(f"{BASE_URL}/crm/v3/objects/deals/batch/upsert").mock(
        side_effect=httpx.ConnectError("x")
    )
    respx.patch(f"{BASE_URL}/crm/v3/objects/contacts/5").respond(200, json={})
    with httpx.Client() as client:
        adapter = HubSpotAdapter("pat-test", client=client)
        with pytest.raises(CRMError, match="429"):
            adapter.upsert_contact({"email": "a@example.com"}, external_id=None)
        with pytest.raises(CRMError, match="transport"):
            adapter.upsert_deal({"external_lead_id": "L1"}, contact_id="1", external_id=None)
        assert adapter.upsert_contact({}, external_id="5") == "5"  # falls back to the known id
        with pytest.raises(CRMError, match="no record id"):
            HubSpotAdapter._id({}, None)

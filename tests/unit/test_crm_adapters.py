"""GoHighLevel and Salesforce: mapping, MOCK idempotent upserts, skeleton request shapes."""

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest
import respx
from pydantic import SecretStr, ValidationError
from sqlalchemy.orm import Session

from app.crm.base import CRMError, NotConfigured
from app.crm.gohighlevel import (
    API_VERSION,
    GoHighLevelAdapter,
    contact_fields,
    opportunity_fields,
    parse_stage_ids,
)
from app.crm.gohighlevel import (
    BASE_URL as GHL_URL,
)
from app.crm.mock_gohighlevel import MockGoHighLevelAdapter
from app.crm.mock_salesforce import MockSalesforceAdapter
from app.crm.salesforce import (
    EXTERNAL_ID_FIELD,
    SalesforceAdapter,
    lead_fields,
)
from app.crm.salesforce import opportunity_fields as sf_opportunity_fields
from app.crm.sync import build_crm_adapter, run_crm_sync
from app.db import init_db, make_engine, make_session_factory
from app.models import Lead
from tests.conftest import make_settings

T0 = datetime(2026, 3, 2, 15, tzinfo=UTC)
STAGES = "new=s1,qualified=s2,contacted=s3,won=s4,lost=s5"
SF_URL = "https://copperline-demo.my.salesforce.com"
SF_LEAD = f"{SF_URL}/services/data/v61.0/sobjects/Lead/{EXTERNAL_ID_FIELD}/"
SF_OPP = f"{SF_URL}/services/data/v61.0/sobjects/Opportunity/{EXTERNAL_ID_FIELD}/"


def _lead(**kw: object) -> Lead:
    base: dict[str, object] = {
        "id": uuid.UUID(int=7),
        "name": "Maya de la Cruz",
        "email": "maya@example.com",
        "phone": "+12025550133",
        "source": "web_form",
        "stage": "new",
        "created_at": T0,
    }
    base.update(kw)
    return Lead(**base)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = make_engine("sqlite://")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s
    engine.dispose()


def _ghl(client: httpx.Client | None = None, **kw: object) -> GoHighLevelAdapter:
    opts: dict[str, object] = {
        "location_id": "loc-1",
        "pipeline_id": "pipe-1",
        "stage_ids": parse_stage_ids(STAGES),
        "client": client,
    }
    opts.update(kw)
    return GoHighLevelAdapter("ghl-test-token", **opts)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- selection


def test_factory_picks_each_provider_and_its_mock() -> None:
    assert isinstance(
        build_crm_adapter(make_settings(crm_provider="gohighlevel")), MockGoHighLevelAdapter
    )
    assert isinstance(
        build_crm_adapter(make_settings(crm_provider="salesforce")), MockSalesforceAdapter
    )
    for provider in ("gohighlevel", "salesforce"):
        with pytest.raises(NotConfigured):
            build_crm_adapter(make_settings(crm_provider=provider, crm_mock=False))
    ghl = build_crm_adapter(
        make_settings(
            crm_provider="gohighlevel",
            crm_mock=False,
            ghl_token=SecretStr("t"),
            ghl_location_id="loc",
            ghl_pipeline_id="pipe",
            ghl_stage_ids=STAGES,
        )
    )
    assert isinstance(ghl, GoHighLevelAdapter)
    with pytest.raises(NotConfigured, match="missing"):
        build_crm_adapter(
            make_settings(
                crm_provider="gohighlevel",
                crm_mock=False,
                ghl_token=SecretStr("t"),
                ghl_location_id="loc",
                ghl_pipeline_id="pipe",
                ghl_stage_ids="new=s1",
            )
        )
    sf = build_crm_adapter(
        make_settings(
            crm_provider="salesforce",
            crm_mock=False,
            salesforce_access_token=SecretStr("t"),
            salesforce_instance_url=SF_URL + "/",
        )
    )
    assert isinstance(sf, SalesforceAdapter)
    with pytest.raises(ValidationError, match="SALESFORCE_INSTANCE_URL"):
        make_settings(salesforce_instance_url="https://login.evil.example.net")
    with pytest.raises(ValidationError):
        make_settings(salesforce_api_version="latest")
    assert make_settings(salesforce_instance_url="").salesforce_instance_url is None


def test_mocks_are_labeled() -> None:
    assert MockGoHighLevelAdapter.name == "MOCK-gohighlevel"
    assert MockSalesforceAdapter.name == "MOCK-salesforce"


# --------------------------------------------------------------------------- GoHighLevel


def test_ghl_mapping() -> None:
    c = contact_fields(_lead())
    assert (c["firstName"], c["lastName"]) == ("Maya", "de la Cruz")
    assert c["email"] == "maya@example.com"
    assert c["external_lead_id"] == str(uuid.UUID(int=7))
    o = opportunity_fields(_lead(stage="won", score=80))
    assert (o["status"], o["stage"], o["lead_score"]) == ("won", "won", "80")
    bare = contact_fields(_lead(email=None, phone=None))
    assert "email" not in bare
    assert "phone" not in bare
    assert opportunity_fields(_lead(stage="lost"))["status"] == "lost"
    with pytest.raises(ValueError, match="missing: contacted, won, lost"):
        parse_stage_ids("new=a, qualified=b,")
    with pytest.raises(ValueError, match="bad GHL_STAGE_IDS"):
        parse_stage_ids("bogus=1")


def test_ghl_mock_is_idempotent_and_refuses_duplicates() -> None:
    crm = MockGoHighLevelAdapter()
    lead = _lead()
    cid = crm.upsert_contact(crm.map_contact(lead), external_id=None)
    again = crm.upsert_contact({"phone": "+12025550133", "firstName": "M"}, external_id=None)
    assert again == cid  # matched by phone
    assert crm.upsert_contact({"email": "MAYA@example.com"}, external_id=None) == cid
    assert crm.upsert_contact({"firstName": "Maya"}, external_id=cid) == cid
    oid = crm.upsert_deal(crm.map_deal(lead), contact_id=cid, external_id=None)
    lead.stage = "qualified"
    assert crm.upsert_deal(crm.map_deal(lead), contact_id=cid, external_id=None) == oid
    assert len(crm.contacts) == 1
    assert len(crm.opportunities) == 1
    assert crm.opportunities[oid].properties["stage"] == "qualified"
    with pytest.raises(CRMError, match="exists"):
        crm.create_opportunity({"name": "x"}, contact_id=cid)
    with pytest.raises(CRMError, match="email or phone"):
        crm.upsert_contact({"firstName": "A"}, external_id=None)
    with pytest.raises(CRMError, match="required"):
        crm.upsert_deal({"name": "x"}, contact_id=cid, external_id=None)
    other = crm.create_opportunity({"name": "y"}, contact_id="someone-else")
    assert other in crm.opportunities
    # Memory reset (worker restart): known ids are reused, nothing duplicated.
    fresh = MockGoHighLevelAdapter()
    assert fresh.upsert_contact({"firstName": "M"}, external_id=cid) == cid
    assert fresh.upsert_deal(crm.map_deal(lead), contact_id=cid, external_id=oid) == oid
    assert ("POST", "/opportunities/upsert") in crm.calls


def test_ghl_request_shapes() -> None:
    adapter = _ghl()
    fields = contact_fields(_lead())
    method, path, body = adapter.contact_request(fields, None)
    assert (method, path) == ("POST", "/contacts/upsert")
    assert body["locationId"] == "loc-1"
    assert body["customFields"] == [
        {"key": "external_lead_id", "field_value": str(uuid.UUID(int=7))}
    ]
    assert adapter.contact_request(fields, "c9")[:2] == ("PUT", "/contacts/c9")
    with pytest.raises(CRMError, match="email or phone"):
        adapter.contact_request(contact_fields(_lead(email=None, phone=None)), None)
    method, path, body = adapter.opportunity_request(
        opportunity_fields(_lead(stage="contacted")), "c9"
    )
    assert (method, path) == ("POST", "/opportunities/upsert")
    assert (body["pipelineId"], body["pipelineStageId"], body["contactId"]) == (
        "pipe-1",
        "s3",
        "c9",
    )
    with pytest.raises(NotConfigured, match="no pipeline stage"):
        _ghl(stage_ids={}).opportunity_request({"stage": "new"}, "c9")


@respx.mock
def test_ghl_wire_format_and_errors() -> None:
    upsert = respx.post(f"{GHL_URL}/contacts/upsert").respond(
        200, json={"contact": {"id": "c1"}, "new": True}
    )
    put = respx.put(f"{GHL_URL}/contacts/c1").respond(200, json={"contact": {"id": "c1"}})
    opp = respx.post(f"{GHL_URL}/opportunities/upsert").respond(
        200, json={"opportunity": {"id": "o1"}, "new": False}
    )
    with httpx.Client() as client:
        adapter = _ghl(client)
        lead = _lead()
        assert adapter.upsert_contact(adapter.map_contact(lead), external_id=None) == "c1"
        assert adapter.upsert_contact(adapter.map_contact(lead), external_id="c1") == "c1"
        assert (
            adapter.upsert_deal(adapter.map_deal(lead), contact_id="c1", external_id=None) == "o1"
        )
        assert (
            adapter.upsert_deal(adapter.map_deal(lead), contact_id="c1", external_id="o1") == "o1"
        )
    req = upsert.calls.last.request
    assert req.headers["authorization"] == "Bearer ghl-test-token"
    assert req.headers["version"] == API_VERSION
    assert json.loads(req.content)["email"] == "maya@example.com"
    assert put.call_count == 1
    assert opp.call_count == 2  # the same upsert both times: one opportunity

    respx.post(f"{GHL_URL}/contacts/upsert").respond(422)
    with httpx.Client() as client:
        adapter = _ghl(client)
        with pytest.raises(CRMError, match="gohighlevel POST -> 422"):
            adapter.upsert_contact({"email": "a@example.com"}, external_id=None)
    respx.post(f"{GHL_URL}/opportunities/upsert").respond(200, json={})
    with httpx.Client() as client, pytest.raises(CRMError, match="no opportunity id"):
        _ghl(client).upsert_deal(opportunity_fields(_lead()), contact_id="c1", external_id=None)


def test_ghl_skeleton_refuses_without_configuration() -> None:
    with pytest.raises(NotConfigured, match="GHL_TOKEN"):
        GoHighLevelAdapter(None, location_id=None, pipeline_id=None, stage_ids=None).upsert_contact(
            {"email": "a@example.com"}, external_id=None
        )
    with pytest.raises(NotConfigured, match="HTTP client"):
        _ghl(None).upsert_contact({"email": "a@example.com"}, external_id=None)


# --------------------------------------------------------------------------- Salesforce


def test_salesforce_mapping() -> None:
    f = lead_fields(_lead())
    assert (f["FirstName"], f["LastName"], f["Company"]) == ("Maya", "de la Cruz", "(individual)")
    assert f["Status"] == "Open - Not Contacted"
    assert f[EXTERNAL_ID_FIELD] == str(uuid.UUID(int=7))
    assert lead_fields(_lead(name="Cher"))["LastName"] == "Cher"
    assert "Email" not in lead_fields(_lead(email=None, phone=None))
    o = sf_opportunity_fields(_lead(stage="lost", lost_reason="went with a cheaper bid"))
    assert (o["StageName"], o["CloseDate"]) == ("Closed Lost", "2026-04-01")
    assert o["Loss_Reason__c"] == "went with a cheaper bid"
    assert "Loss_Reason__c" not in sf_opportunity_fields(_lead(stage="won"))


def test_salesforce_mock_upserts_by_external_id() -> None:
    crm = MockSalesforceAdapter()
    lead = _lead()
    lid = crm.upsert_contact(crm.map_contact(lead), external_id=None)
    assert lid.startswith("00Q")
    assert crm.upsert_contact(crm.map_contact(lead), external_id=lid) == lid
    oid = crm.upsert_deal(crm.map_deal(lead), contact_id=lid, external_id=None)
    lead.stage = "won"
    assert crm.upsert_deal(crm.map_deal(lead), contact_id=lid, external_id=None) == oid
    assert oid.startswith("006")
    assert len(crm.records["Lead"]) == 1
    assert len(crm.records["Opportunity"]) == 1
    opp = crm.records["Opportunity"][oid]
    assert opp.properties["StageName"] == "Closed Won"
    assert opp.properties["Lead__c"] == lid
    assert opp.updates == 1
    with pytest.raises(CRMError, match="DUPLICATE_VALUE"):
        crm.insert("Lead", crm.map_contact(lead))
    assert crm.insert("Lead", {EXTERNAL_ID_FIELD: "other"}).startswith("00Q")
    with pytest.raises(CRMError, match="required"):
        crm.upsert_contact({"LastName": "x"}, external_id=None)


def _sf(client: httpx.Client | None) -> SalesforceAdapter:
    return SalesforceAdapter("sf-test-token", instance_url=SF_URL, client=client)


def test_salesforce_request_shape() -> None:
    method, path, body = _sf(None).upsert_request("Lead", lead_fields(_lead()))
    assert method == "PATCH"
    assert path == f"/services/data/v61.0/sobjects/Lead/{EXTERNAL_ID_FIELD}/{uuid.UUID(int=7)}"
    assert EXTERNAL_ID_FIELD not in body  # it is in the URL
    with pytest.raises(CRMError, match="required"):
        _sf(None).upsert_request("Lead", {"LastName": "x"})


@respx.mock
def test_salesforce_wire_format() -> None:
    external = str(uuid.UUID(int=7))
    lead_route = respx.patch(SF_LEAD + external).mock(
        side_effect=[
            httpx.Response(201, json={"id": "00Q1", "success": True, "created": True}),
            httpx.Response(200, json={"id": "00Q1", "success": True, "created": False}),
        ]
    )
    opp_route = respx.patch(SF_OPP + external).respond(204)
    read_back = respx.get(SF_OPP + external + "?fields=Id").respond(200, json={"Id": "0061"})
    with httpx.Client() as client:
        adapter = _sf(client)
        lead = _lead()
        assert adapter.upsert_contact(adapter.map_contact(lead), external_id=None) == "00Q1"
        assert adapter.upsert_contact(adapter.map_contact(lead), external_id="00Q1") == "00Q1"
        # 204 without a body: read the id back by external id, or use the saved one.
        assert (
            adapter.upsert_deal(adapter.map_deal(lead), contact_id="00Q1", external_id=None)
            == "0061"
        )
        assert (
            adapter.upsert_deal(adapter.map_deal(lead), contact_id="00Q1", external_id="0061")
            == "0061"
        )
    req = lead_route.calls.last.request
    assert req.headers["authorization"] == "Bearer sf-test-token"
    assert json.loads(req.content)["Email"] == "maya@example.com"
    assert json.loads(opp_route.calls.last.request.content)["Lead__c"] == "00Q1"
    assert read_back.call_count == 1


@respx.mock
def test_salesforce_errors() -> None:
    external = str(uuid.UUID(int=7))
    respx.patch(SF_LEAD + external).respond(200, json={"success": False})
    respx.patch(SF_OPP + external).respond(204)
    respx.get(SF_OPP + external + "?fields=Id").respond(200, json={})
    with httpx.Client() as client:
        adapter = _sf(client)
        with pytest.raises(CRMError, match="not successful"):
            adapter.upsert_contact(lead_fields(_lead()), external_id=None)
        with pytest.raises(CRMError, match="no id"):
            adapter.upsert_deal(sf_opportunity_fields(_lead()), contact_id="x", external_id=None)
    with pytest.raises(NotConfigured, match="SALESFORCE_ACCESS_TOKEN"):
        SalesforceAdapter(None, instance_url=None).upsert_contact(
            lead_fields(_lead()), external_id=None
        )


# --------------------------------------------------------------------------- sync job


@pytest.mark.parametrize("adapter_cls", [MockGoHighLevelAdapter, MockSalesforceAdapter])
def test_retried_sync_does_not_duplicate(session: Session, adapter_cls: type) -> None:
    lead = _lead(id=uuid.uuid4())
    session.add(lead)
    session.commit()
    crm = adapter_cls()
    run_crm_sync(session, lead.id, crm, now=T0)
    session.rollback()  # the job failed after the CRM calls: saved ids roll back
    assert lead.crm_deal_id is None
    run_crm_sync(session, lead.id, crm, now=T0)
    lead.stage = "qualified"
    run_crm_sync(session, lead.id, crm, now=T0)
    session.commit()
    if isinstance(crm, MockGoHighLevelAdapter):
        assert (len(crm.contacts), len(crm.opportunities)) == (1, 1)
    else:
        assert (len(crm.records["Lead"]), len(crm.records["Opportunity"])) == (1, 1)
    assert lead.crm_contact_id
    assert lead.crm_deal_id


@respx.mock
def test_transport_errors_become_crm_errors_without_payloads() -> None:
    respx.post(f"{GHL_URL}/contacts/upsert").mock(side_effect=httpx.ConnectError("down"))
    with httpx.Client() as client, pytest.raises(CRMError) as info:
        _ghl(client).upsert_contact({"email": "maya@example.com"}, external_id=None)
    assert "transport error: ConnectError" in str(info.value)
    assert "maya" not in str(info.value)

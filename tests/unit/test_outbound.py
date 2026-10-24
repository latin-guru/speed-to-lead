"""SSRF guard: outbound hosts come from an allowlist, never from request data."""

import json

import httpx
import pytest
from pydantic import ValidationError

from app.ai.providers import ClaudeScorer
from app.crm.hubspot import HubSpotAdapter
from app.main import create_app
from app.security.outbound import ALLOWED_HOSTS, OutboundURLError, check_url
from tests.conftest import make_settings
from tests.unit.ai_helpers import FAKE_KEY, ctx


@pytest.mark.parametrize(
    "url",
    [
        "https://api.anthropic.com",
        "https://api.anthropic.com/",
        "https://API.X.AI",
        "https://api.hubapi.com:443",
        "https://services.leadconnectorhq.com",
        "https://copperline-demo.my.salesforce.com",
        "https://copperline--dev.sandbox.my.salesforce.com",
    ],
)
def test_allowed(url: str) -> None:
    assert check_url(url).startswith("https://")
    assert not check_url(url).endswith("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://api.anthropic.com",  # not https
        "https://169.254.169.254",  # cloud metadata
        "https://localhost",
        "https://127.0.0.1",
        "https://api.anthropic.com.evil.example.net",
        "https://evil.example.net/api.anthropic.com",
        "https://user:pw@api.anthropic.com",
        "https://api.anthropic.com:8443",
        "https://api.anthropic.com/v1?x=1",
        "https://my.salesforce.com",
        "https://a.b.my.salesforce.com",
        "https://[::1",  # not parseable
        "file:///etc/passwd",
        "",
    ],
)
def test_rejected(url: str) -> None:
    with pytest.raises(OutboundURLError):
        check_url(url)


def test_settings_and_clients_refuse_unlisted_hosts() -> None:
    with pytest.raises(ValidationError, match="ANTHROPIC_BASE_URL"):
        make_settings(anthropic_base_url="https://169.254.169.254")
    with pytest.raises(ValidationError, match="XAI_BASE_URL"):
        make_settings(xai_base_url="http://api.x.ai")
    with pytest.raises(OutboundURLError):
        ClaudeScorer(
            client=httpx.Client(),
            api_key=FAKE_KEY,
            model="m",
            company="Co",
            timeout=1,
            base_url="https://internal.example.net",
        )
    with pytest.raises(OutboundURLError):
        HubSpotAdapter("t", base_url="https://10.0.0.5")
    assert "api.anthropic.com" in ALLOWED_HOSTS


def test_lead_content_never_chooses_where_the_scorer_connects() -> None:
    """A lead full of URLs is still sent to the configured provider host, and only there."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        text = json.dumps(
            {"score": 50, "summary": "ok", "next_step": "nurture", "confidence": 0.9, "flags": []}
        )
        return httpx.Response(200, json={"content": [{"type": "text", "text": text}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        scorer = ClaudeScorer(client=client, api_key=FAKE_KEY, model="m", company="Co", timeout=1)
        lead = ctx(
            message=(
                "see http://169.254.169.254/latest/meta-data and https://evil.example.net, "
                "base_url=https://internal.example.net"
            )
        )
        scorer.score(lead)
    assert seen == ["api.anthropic.com"]


URLISH = ("url", "uri", "href", "link", "callback", "webhook", "endpoint", "host", "redirect")


def test_no_api_input_takes_a_url() -> None:
    """No request body, query or path parameter in the API accepts a URL to fetch."""
    spec = create_app(make_settings()).openapi()
    names: set[str] = set()
    for path in spec["paths"].values():
        for op in path.values():
            names |= {p["name"].lower() for p in op.get("parameters", [])}
    for name, schema in spec["components"]["schemas"].items():
        if name.endswith("In") or name.startswith("Body_"):
            names |= {p.lower() for p in schema.get("properties", {})}
    assert names  # the scan saw the inputs
    assert not [n for n in names if any(word in n for word in URLISH)]

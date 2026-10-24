import json
from collections.abc import Iterator

import httpx
import pytest
import respx

from app.ai.base import InvalidOutputError, ProviderError, RetryableProviderError
from app.ai.providers import ClaudeScorer, GrokScorer
from tests.unit.ai_helpers import FAKE_KEY, ctx, model_json

CLAUDE_URL = "https://api.anthropic.com/v1/messages"
GROK_URL = "https://api.x.ai/v1/chat/completions"


@pytest.fixture
def http() -> Iterator[httpx.Client]:
    with httpx.Client() as c:
        yield c


def claude(http: httpx.Client) -> ClaudeScorer:
    return ClaudeScorer(
        client=http,
        api_key=FAKE_KEY,
        model="claude-test",
        company="Co",
        timeout=1,
        base_url="https://api.anthropic.com",
    )


def grok(http: httpx.Client) -> GrokScorer:
    return GrokScorer(
        client=http,
        api_key=FAKE_KEY,
        model="grok-test",
        company="Co",
        timeout=1,
        base_url="https://api.x.ai",
    )


def claude_body(text: str, stop: str = "end_turn") -> dict[str, object]:
    return {"content": [{"type": "text", "text": text}], "stop_reason": stop}


@respx.mock
def test_claude_success_and_request_shape(http: httpx.Client) -> None:
    route = respx.post(CLAUDE_URL).respond(200, json=claude_body(model_json()))
    result = claude(http).score(ctx())
    assert result.score == 82
    req = route.calls.last.request
    assert req.headers["x-api-key"] == FAKE_KEY
    assert req.headers["anthropic-version"] == "2023-06-01"
    sent = json.loads(req.content)
    assert sent["model"] == "claude-test"
    assert "tools" not in sent  # model can only answer with text
    assert "untrusted" in sent["system"]


@respx.mock
@pytest.mark.parametrize("status", [429, 500, 503, 529])
def test_claude_retryable_statuses(http: httpx.Client, status: int) -> None:
    respx.post(CLAUDE_URL).respond(status, headers={"retry-after": "1"})
    with pytest.raises(RetryableProviderError) as info:
        claude(http).score(ctx())
    assert info.value.retry_after == 1.0


@respx.mock
def test_claude_client_error_not_retryable(http: httpx.Client) -> None:
    respx.post(CLAUDE_URL).respond(401, json={"error": "nope"})
    with pytest.raises(ProviderError) as info:
        claude(http).score(ctx())
    assert not isinstance(info.value, RetryableProviderError)


@respx.mock
def test_timeout_and_connect_errors_are_retryable(http: httpx.Client) -> None:
    respx.post(CLAUDE_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(RetryableProviderError):
        claude(http).score(ctx())
    respx.post(CLAUDE_URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(RetryableProviderError):
        claude(http).score(ctx())


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {
            "content": [{"type": "tool_use", "name": "update_stage", "input": {}}],
            "stop_reason": "tool_use",
        },
        {
            "content": [{"type": "text", "text": "x"}, {"type": "tool_use"}],
            "stop_reason": "end_turn",
        },
        {"content": "not-a-list"},
        {"content": [{"type": "text", "text": "I think this lead is great!"}]},
    ],
)
def test_claude_rejects_non_schema_output(http: httpx.Client, body: dict[str, object]) -> None:
    respx.post(CLAUDE_URL).respond(200, json=body)
    with pytest.raises(InvalidOutputError):
        claude(http).score(ctx())


@respx.mock
def test_non_json_envelope(http: httpx.Client) -> None:
    respx.post(CLAUDE_URL).respond(200, text="<html>gateway</html>")
    with pytest.raises(InvalidOutputError):
        claude(http).score(ctx())
    respx.post(CLAUDE_URL).respond(200, json=[1, 2])
    with pytest.raises(InvalidOutputError):
        claude(http).score(ctx())


@respx.mock
def test_retry_after_unparseable(http: httpx.Client) -> None:
    respx.post(CLAUDE_URL).respond(503, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})
    with pytest.raises(RetryableProviderError) as info:
        claude(http).score(ctx())
    assert info.value.retry_after is None


@respx.mock
def test_grok_success_and_request_shape(http: httpx.Client) -> None:
    route = respx.post(GROK_URL).respond(
        200, json={"choices": [{"message": {"role": "assistant", "content": model_json(score=40)}}]}
    )
    assert grok(http).score(ctx()).score == 40
    req = route.calls.last.request
    assert req.headers["authorization"] == f"Bearer {FAKE_KEY}"
    sent = json.loads(req.content)
    assert sent["messages"][0]["role"] == "system"
    assert "tools" not in sent
    assert sent["response_format"] == {"type": "json_object"}


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        {"choices": [{"message": {"content": None, "tool_calls": [{"id": "1"}]}}]},
        {"choices": [{"message": {"content": 5}}]},
        {"choices": [{"message": "x"}]},
    ],
)
def test_grok_rejects_bad_envelopes(http: httpx.Client, body: dict[str, object]) -> None:
    respx.post(GROK_URL).respond(200, json=body)
    with pytest.raises(InvalidOutputError):
        grok(http).score(ctx())

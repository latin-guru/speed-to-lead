"""HTTP scorers for Anthropic (Messages API) and xAI (chat completions), via httpx.

Neither request offers tools/functions, so the model can only return text; that text must
parse into ``ScoreResult`` or it is discarded.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.ai.base import InvalidOutputError, ProviderError, RetryableProviderError
from app.ai.guard import build_prompt
from app.ai.parsing import parse_score_output
from app.ai.schemas import LeadContext, ScoreResult

MAX_OUTPUT_TOKENS = 400


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class _HTTPScorer:
    name = "http"

    def __init__(self, *, client: httpx.Client, company: str, timeout: float) -> None:
        self._client = client
        self._company = company
        self._timeout = timeout

    def _post(self, url: str, *, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.post(url, headers=headers, json=body, timeout=self._timeout)
        except httpx.TimeoutException as exc:
            raise RetryableProviderError(f"{self.name} timed out") from exc
        except httpx.TransportError as exc:
            raise RetryableProviderError(
                f"{self.name} transport error: {type(exc).__name__}"
            ) from exc
        status = response.status_code
        if status == 429 or status >= 500:
            raise RetryableProviderError(
                f"{self.name} returned {status}", retry_after=_retry_after(response)
            )
        if status >= 400:
            raise ProviderError(f"{self.name} returned {status}")
        try:
            data = response.json()
        except ValueError as exc:
            raise InvalidOutputError(f"{self.name} returned a non-JSON envelope") from exc
        if not isinstance(data, dict):
            raise InvalidOutputError(f"{self.name} returned an unexpected envelope")
        return data


class ClaudeScorer(_HTTPScorer):
    name = "claude"

    def __init__(
        self,
        *,
        client: httpx.Client,
        api_key: str,
        model: str,
        company: str,
        timeout: float,
        base_url: str = "https://api.anthropic.com",
    ) -> None:
        super().__init__(client=client, company=company, timeout=timeout)
        self._api_key = api_key
        self._model = model
        self._url = base_url.rstrip("/") + "/v1/messages"

    def score(self, lead: LeadContext) -> ScoreResult:
        system, user = build_prompt(lead, self._company)
        data = self._post(
            self._url,
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            body={
                "model": self._model,
                "max_tokens": MAX_OUTPUT_TOKENS,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )
        blocks = data.get("content")
        if not isinstance(blocks, list) or data.get("stop_reason") == "tool_use":
            raise InvalidOutputError("claude response has no text content")
        if any(
            not isinstance(b, dict) or b.get("type") not in ("text", "thinking") for b in blocks
        ):
            raise InvalidOutputError("claude response contained non-text blocks")
        text = "".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text")
        return parse_score_output(text)


class GrokScorer(_HTTPScorer):
    name = "grok"

    def __init__(
        self,
        *,
        client: httpx.Client,
        api_key: str,
        model: str,
        company: str,
        timeout: float,
        base_url: str = "https://api.x.ai",
    ) -> None:
        super().__init__(client=client, company=company, timeout=timeout)
        self._api_key = api_key
        self._model = model
        self._url = base_url.rstrip("/") + "/v1/chat/completions"

    def score(self, lead: LeadContext) -> ScoreResult:
        system, user = build_prompt(lead, self._company)
        data = self._post(
            self._url,
            headers={
                "authorization": f"Bearer {self._api_key}",
                "content-type": "application/json",
            },
            body={
                "model": self._model,
                "max_tokens": MAX_OUTPUT_TOKENS,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise InvalidOutputError("grok response has no message") from exc
        if not isinstance(message, dict) or message.get("tool_calls"):
            raise InvalidOutputError("grok response attempted tool calls")
        content = message.get("content")
        if not isinstance(content, str):
            raise InvalidOutputError("grok response content is not text")
        return parse_score_output(content)

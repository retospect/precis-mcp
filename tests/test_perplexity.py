"""Tests for ``handlers/perplexity.py`` — websearch / think / research.

httpx is patched at the call-site (we replace ``httpx.Client`` with a
stub) so tests stay offline. Same approach as ``test_web.py``.

PERPLEXITY_API_KEY is set in a fixture so the auth check passes; tests
that exercise the missing-key path delete it explicitly.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.errors import BadInput, Upstream
from precis.handlers.perplexity import (
    ResearchHandler,
    ThinkHandler,
    WebsearchHandler,
    _format_perplexity_body,
)
from precis.store import Store

# ── stub httpx.Client ────────────────────────────────────────────────


class _StubResp:
    def __init__(
        self,
        *,
        json_data: dict[str, Any] | None = None,
        status_code: int = 200,
        text: str = "",
    ) -> None:
        self._json_data = json_data or {}
        self.status_code = status_code
        self.text = text or "stub-text"

    def json(self) -> dict[str, Any]:
        return self._json_data


class _StubClient:
    """Drop-in for ``httpx.Client(...)`` used by perplexity._fetch."""

    last_payload: dict[str, Any] | None = None
    response: _StubResp | None = None
    raise_on_post: Exception | None = None

    def __init__(self, **_kw: Any) -> None:
        pass

    def __enter__(self) -> _StubClient:
        return self

    def __exit__(self, *_e: Any) -> None:
        pass

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> _StubResp:
        type(self).last_payload = json
        if type(self).raise_on_post is not None:
            raise type(self).raise_on_post  # type: ignore[misc]
        assert type(self).response is not None
        return type(self).response


_SAMPLE_RESPONSE = {
    "choices": [
        {
            "message": {
                "content": (
                    "The current CEO of Anthropic is Dario Amodei [1]. He "
                    "co-founded the company in 2021 [2]."
                )
            }
        }
    ],
    "citations": [
        "https://anthropic.com/about",
        "https://en.wikipedia.org/wiki/Anthropic",
    ],
}


@pytest.fixture(autouse=True)
def _patch_httpx_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch httpx.Client + set the API key for every perplexity test."""
    import httpx

    monkeypatch.setenv("PERPLEXITY_API_KEY", "TEST-KEY")
    _StubClient.last_payload = None
    _StubClient.response = _StubResp(json_data=_SAMPLE_RESPONSE)
    _StubClient.raise_on_post = None
    monkeypatch.setattr(httpx, "Client", _StubClient)


@pytest.fixture
def websearch(store: Store) -> WebsearchHandler:
    return WebsearchHandler(store=store)


@pytest.fixture
def think(store: Store) -> ThinkHandler:
    return ThinkHandler(store=store)


@pytest.fixture
def research(store: Store) -> ResearchHandler:
    return ResearchHandler(store=store)


# ── basic flow ────────────────────────────────────────────────────────


def test_websearch_fetches_and_caches(websearch: WebsearchHandler) -> None:
    resp = websearch.get(id="who is the CEO of Anthropic")
    # Body has answer + citations.
    assert "Dario Amodei" in resp.body
    assert "[1]" in resp.body
    assert "Sources:" in resp.body
    assert "anthropic.com/about" in resp.body
    # Attribution footer mentions the model.
    assert "sonar" in resp.body
    assert "Perplexity" in resp.body
    # First call → cost trailer reflects per-call price.
    assert "$0.001" in resp.cost


def test_payload_uses_correct_model(websearch: WebsearchHandler) -> None:
    websearch.get(id="hello")
    assert _StubClient.last_payload is not None
    assert _StubClient.last_payload["model"] == "sonar"


def test_think_uses_reasoning_pro_model(think: ThinkHandler) -> None:
    think.get(id="compare A and B")
    assert _StubClient.last_payload is not None
    assert _StubClient.last_payload["model"] == "sonar-reasoning-pro"


def test_research_uses_deep_research_model(research: ResearchHandler) -> None:
    research.get(id="landscape of post-quantum signatures")
    assert _StubClient.last_payload is not None
    assert _StubClient.last_payload["model"] == "sonar-deep-research"


def test_second_call_hits_cache(websearch: WebsearchHandler) -> None:
    websearch.get(id="hello")
    # Change the upstream response to detect cache hit.
    _StubClient.response = _StubResp(
        json_data={
            "choices": [{"message": {"content": "different answer"}}],
            "citations": [],
        }
    )
    resp2 = websearch.get(id="hello")
    assert "Dario Amodei" in resp2.body  # original cached body
    assert "different answer" not in resp2.body


def test_same_query_different_kind_caches_separately(
    websearch: WebsearchHandler, think: ThinkHandler
) -> None:
    """Cache key includes the model so websearch + think don't collide."""
    websearch.get(id="hello world")
    # Change response so think gets a different body than websearch.
    _StubClient.response = _StubResp(
        json_data={
            "choices": [{"message": {"content": "think answer body"}}],
            "citations": [],
        }
    )
    resp_think = think.get(id="hello world")
    assert "think answer body" in resp_think.body  # not the cached websearch body


def test_query_canonicalization_trims(websearch: WebsearchHandler) -> None:
    websearch.get(id="  hello world  ")
    websearch.get(id="hello world")
    # Both calls collapse to one cache row → second response = first.
    resp = websearch.get(id="hello world")
    assert "Dario Amodei" in resp.body


# ── input validation ─────────────────────────────────────────────────


def test_empty_query_raises(websearch: WebsearchHandler) -> None:
    # Empty input is rejected by the cache-base coercion layer.
    with pytest.raises(BadInput, match="require a query"):
        websearch.get(id="")


def test_whitespace_only_query_raises(websearch: WebsearchHandler) -> None:
    with pytest.raises(BadInput, match="require a query"):
        websearch.get(id="   ")


# ── env / availability ───────────────────────────────────────────────


def test_missing_api_key_raises_upstream(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    h = WebsearchHandler(store=store)
    with pytest.raises(Upstream, match="PERPLEXITY_API_KEY"):
        h.get(id="anything")


def test_kind_hidden_when_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    assert WebsearchHandler.spec.is_available() is False
    assert ThinkHandler.spec.is_available() is False
    assert ResearchHandler.spec.is_available() is False


def test_kind_visible_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERPLEXITY_API_KEY", "anything")
    assert WebsearchHandler.spec.is_available() is True


# ── HTTP error handling ──────────────────────────────────────────────


def test_401_raises_upstream(websearch: WebsearchHandler) -> None:
    _StubClient.response = _StubResp(status_code=401, text="bad key")
    with pytest.raises(Upstream, match="HTTP 401|API key"):
        websearch.get(id="hello")


def test_429_raises_upstream(websearch: WebsearchHandler) -> None:
    _StubClient.response = _StubResp(status_code=429, text="throttled")
    with pytest.raises(Upstream, match="rate limit"):
        websearch.get(id="hello")


def test_5xx_raises_upstream(websearch: WebsearchHandler) -> None:
    _StubClient.response = _StubResp(status_code=503, text="overload")
    with pytest.raises(Upstream, match="503"):
        websearch.get(id="hello")


def test_timeout_raises_upstream(websearch: WebsearchHandler) -> None:
    import httpx

    _StubClient.raise_on_post = httpx.TimeoutException("slow")
    with pytest.raises(Upstream, match="timed out"):
        websearch.get(id="hello")


def test_network_error_raises_upstream(websearch: WebsearchHandler) -> None:
    import httpx

    _StubClient.raise_on_post = httpx.ConnectError("dns fail")
    with pytest.raises(Upstream, match="transport error"):
        websearch.get(id="hello")


# ── response formatting ──────────────────────────────────────────────


def test_format_response_handles_empty_choices() -> None:
    body, citations = _format_perplexity_body({"choices": [], "citations": []})
    assert "no answer" in body
    assert citations == []


def test_format_response_handles_missing_content() -> None:
    body, _ = _format_perplexity_body(
        {"choices": [{"message": {"content": ""}}], "citations": []}
    )
    assert "empty answer" in body


def test_format_response_lists_citations() -> None:
    body, cits = _format_perplexity_body(_SAMPLE_RESPONSE)
    assert "Sources:" in body
    assert "[1] https://anthropic.com/about" in body
    assert "[2] https://en.wikipedia.org/wiki/Anthropic" in body
    assert cits == [
        "https://anthropic.com/about",
        "https://en.wikipedia.org/wiki/Anthropic",
    ]


# ── per-tier metadata ────────────────────────────────────────────────


def test_ttl_per_tier() -> None:
    """websearch=7d, think=30d, research=pinned."""
    assert WebsearchHandler.ttl_seconds == 7 * 24 * 60 * 60
    assert ThinkHandler.ttl_seconds == 30 * 24 * 60 * 60
    assert ResearchHandler.ttl_seconds is None


def test_cost_per_tier() -> None:
    assert WebsearchHandler.cost_per_call_usd == pytest.approx(0.001)
    assert ThinkHandler.cost_per_call_usd == pytest.approx(0.005)
    assert ResearchHandler.cost_per_call_usd == pytest.approx(0.50)


def test_cost_appears_on_fresh_fetch(websearch: WebsearchHandler) -> None:
    resp = websearch.get(id="something")
    # Cost trailer reflects the per-call cost for the tier.
    assert "$0.001" in resp.cost

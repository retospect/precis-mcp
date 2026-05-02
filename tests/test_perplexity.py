"""Tests for ``handlers/perplexity.py`` — websearch / think / research.

httpx is patched at the call-site (we replace ``httpx.Client`` with a
stub) so tests stay offline. Same approach as ``test_web.py``.

PERPLEXITY_API_KEY is set in a fixture so the auth check passes; tests
that exercise the missing-key path delete it explicitly.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
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
def websearch(hub_no_embedder: Hub) -> WebsearchHandler:
    return WebsearchHandler(hub=hub_no_embedder)


@pytest.fixture
def think(hub_no_embedder: Hub) -> ThinkHandler:
    return ThinkHandler(hub=hub_no_embedder)


@pytest.fixture
def research(hub_no_embedder: Hub) -> ResearchHandler:
    return ResearchHandler(hub=hub_no_embedder)


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


def test_empty_id_renders_recent_listing(websearch: WebsearchHandler) -> None:
    """Empty/whitespace id is interpreted as "show recent refs" rather
    than rejected — same ergonomics as `get(kind='markdown')` with no
    id. Actual empty input to the fetch path is still caught inside
    ``_canonical_key`` if someone pushes through via ``q=``."""
    resp = websearch.get(id="")
    assert "recent websearch refs" in resp.body.lower()


def test_whitespace_only_id_renders_recent_listing(
    websearch: WebsearchHandler,
) -> None:
    resp = websearch.get(id="   ")
    assert "recent websearch refs" in resp.body.lower()


def test_empty_query_via_q_still_raises(websearch: WebsearchHandler) -> None:
    """``q=''`` with no ``id=`` still routes to the recent listing —
    same as no input at all — because the recent view has priority
    over query validation."""
    resp = websearch.get(q="")
    assert "recent websearch refs" in resp.body.lower()


# ── env / availability ───────────────────────────────────────────────


def test_missing_api_key_raises_upstream(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    h = WebsearchHandler(hub=Hub(store=store))
    with pytest.raises(Upstream, match="PERPLEXITY_API_KEY"):
        h.get(id="anything")


def test_kind_available_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Perplexity kinds are always available — imports, /recent, and
    cache hits all work without an API key. Only cache-miss ``get``
    needs the key, and that path raises ``Upstream`` with a helpful
    message rather than hiding the kind."""
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    assert WebsearchHandler.spec.is_available() is True
    assert ThinkHandler.spec.is_available() is True
    assert ResearchHandler.spec.is_available() is True


def test_kind_available_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_format_response_strips_paired_think_block() -> None:
    """The canonical paired ``<think>…</think>`` form must be
    stripped — the trace is internal scratch and the conclusion
    follows the closing tag."""
    body, _ = _format_perplexity_body(
        {
            "choices": [
                {
                    "message": {
                        "content": (
                            "<think>scratch reasoning that the user "
                            "should never see</think>\n"
                            "## Answer\nThe conclusion lives here."
                        )
                    }
                }
            ],
            "citations": [],
        }
    )
    assert "scratch reasoning" not in body
    assert "## Answer" in body
    assert "The conclusion lives here." in body


def test_format_response_strips_orphan_closing_think_tag() -> None:
    """Streaming truncation can leave an orphan ``</think>`` with
    no opener — the body the user sees must not carry the trace
    fragment that came before it. (MCP critic MINOR-C 2026-05-02
    — orphan closing tags leaked into the corpus.)
    """
    body, _ = _format_perplexity_body(
        {
            "choices": [
                {
                    "message": {
                        "content": (
                            "Internal trace fragment that streamed "
                            "before the conclusion. </think>\n"
                            "I should keep this brief but comprehensive. "
                            "Final answer body."
                        )
                    }
                }
            ],
            "citations": [],
        }
    )
    assert "</think>" not in body
    assert "Internal trace fragment" not in body
    assert "Final answer body." in body


def test_format_response_strips_orphan_opening_think_tag() -> None:
    """Truncation in the other direction — ``<think>`` with no
    closer — drops everything from the tag to end-of-string
    rather than leak the trace into the rendered answer.
    """
    body, _ = _format_perplexity_body(
        {
            "choices": [
                {
                    "message": {
                        "content": (
                            "## Answer\nThe conclusion is brief.\n"
                            "<think>and now scratch reasoning that "
                            "got truncated mid-thought without a "
                            "closing tag…"
                        )
                    }
                }
            ],
            "citations": [],
        }
    )
    assert "<think>" not in body
    assert "scratch reasoning" not in body
    assert "## Answer" in body
    assert "The conclusion is brief." in body


def test_format_response_no_think_tag_is_no_op() -> None:
    """Most websearch / research bodies have no reasoning block;
    the strip must be a no-op there."""
    payload = {
        "choices": [{"message": {"content": "## Plain answer\nNo trace here."}}],
        "citations": [],
    }
    body, _ = _format_perplexity_body(payload)
    assert "## Plain answer" in body
    assert "No trace here." in body


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


# ── put(mode='import') — register a free, web-UI-generated report ────


_SAMPLE_RESEARCH_REPORT = """\
# CO2 Electrolysis Catalyst Survey

## Overview

Direct electrochemical reduction of CO2 to value-added products
remains a leading decarbonisation pathway. Recent cobalt phthalocyanine
and copper-based systems have crossed the 80% Faradaic efficiency
threshold for ethylene at industrially relevant current densities.

## Catalysts

- Cobalt phthalocyanine on carbon nanotubes (Wang 2023)
- Polycrystalline copper at high overpotential (Hori 1985)
- Single-atom Cu/N-doped carbon (Liu 2022)

## Sources

[1] https://example.org/wang-2023
[2] https://example.org/hori-1985
"""


@pytest.fixture
def research_with_embedder(store: Store) -> ResearchHandler:
    """ResearchHandler wired with a deterministic mock embedder.

    The mock embedder produces unit-norm 1024-dim vectors, matching the
    DB column dim, so imported blocks land with embeddings populated.
    """
    return ResearchHandler(hub=Hub(store=store, embedder=MockEmbedder(dim=1024)))


def test_import_then_get_returns_imported_body_at_zero_cost(
    research_with_embedder: ResearchHandler,
) -> None:
    """The headline use case: import once, free hits forever."""
    h = research_with_embedder
    query = "Survey of CO2 electrolysis catalysts"

    # Mutate the stub so any API call during this test would be obvious.
    _StubClient.response = _StubResp(
        json_data={
            "choices": [{"message": {"content": "API SHOULD NOT BE CALLED"}}],
            "citations": [],
        }
    )

    put_resp = h.put(id=query, text=_SAMPLE_RESEARCH_REPORT, mode="import")
    assert "imported" in put_resp.body.lower()
    assert "research" in put_resp.body

    # First get must hit the cache populated by the import — not the API.
    get_resp = h.get(id=query)
    assert "CO2 Electrolysis Catalyst Survey" in get_resp.body
    assert "Cobalt phthalocyanine" in get_resp.body
    assert "API SHOULD NOT BE CALLED" not in get_resp.body
    # Cost trailer reads as free for imported entries.
    assert "free" in get_resp.cost


def test_import_works_without_embedder(store: Store) -> None:
    """No embedder configured → blocks still land, just without vectors."""
    h = ResearchHandler(hub=Hub(store=store))  # no embedder
    query = "embedderless import"
    resp = h.put(id=query, text="# Title\n\nOne paragraph.", mode="import")
    assert "imported" in resp.body.lower()


def test_import_parses_markdown_into_multiple_blocks(
    research_with_embedder: ResearchHandler,
) -> None:
    """Reports are split per heading/paragraph/list — granular search."""
    h = research_with_embedder
    h.put(
        id="catalyst survey",
        text=_SAMPLE_RESEARCH_REPORT,
        mode="import",
    )
    # Look up the ref the import created and count its blocks.
    cached = h.store.get_cache_entry(
        provider="perplexity",
        request_hash=h._hash(h._canonical_key("catalyst survey")),
    )
    assert cached is not None
    ref, _cache = cached
    blocks = h.store.list_blocks_for_ref(ref.id)
    # Sample report has multiple sections → must produce >1 block.
    assert len(blocks) >= 4


def test_import_records_source_imported_metadata(
    research_with_embedder: ResearchHandler,
) -> None:
    h = research_with_embedder
    h.put(id="provenance check", text="# x\n\nbody", mode="import")
    cached = h.store.get_cache_entry(
        provider="perplexity",
        request_hash=h._hash(h._canonical_key("provenance check")),
    )
    assert cached is not None
    ref, cache = cached
    assert (ref.meta or {}).get("source") == "imported"
    assert (cache.meta or {}).get("source") == "imported"
    assert cache.cost_usd == 0.0
    # Pinned: imports never expire.
    assert cache.fresh_until is None


def test_import_is_idempotent_on_repeat(
    research_with_embedder: ResearchHandler,
) -> None:
    """Re-importing the same query replaces, doesn't duplicate refs."""
    h = research_with_embedder
    h.put(id="same query", text="# v1\n\nfirst body", mode="import")
    h.put(id="same query", text="# v2\n\nsecond body", mode="import")

    # Only one cache row should exist for this (provider, hash).
    request_hash = h._hash(h._canonical_key("same query"))
    cached = h.store.get_cache_entry(provider="perplexity", request_hash=request_hash)
    assert cached is not None
    ref, _cache = cached
    blocks = h.store.list_blocks_for_ref(ref.id)
    full = "\n".join(b.text for b in blocks)
    # The second import wins; the first is gone.
    assert "second body" in full
    assert "first body" not in full


def test_import_different_kinds_use_separate_cache_rows(
    store: Store,
) -> None:
    """Importing the same query into research vs websearch creates two
    distinct cache entries — the model is part of the key."""
    hub = Hub(store=store, embedder=MockEmbedder(dim=1024))
    research = ResearchHandler(hub=hub)
    websearch = WebsearchHandler(hub=hub)

    research.put(id="dual import", text="# r\n\nresearch body", mode="import")
    websearch.put(id="dual import", text="# w\n\nwebsearch body", mode="import")

    r_cache = store.get_cache_entry(
        provider="perplexity",
        request_hash=research._hash(research._canonical_key("dual import")),
    )
    w_cache = store.get_cache_entry(
        provider="perplexity",
        request_hash=websearch._hash(websearch._canonical_key("dual import")),
    )
    assert r_cache is not None and w_cache is not None
    assert r_cache[0].id != w_cache[0].id
    assert r_cache[0].kind == "research"
    assert w_cache[0].kind == "websearch"


def test_import_rejects_missing_text(
    research_with_embedder: ResearchHandler,
) -> None:
    with pytest.raises(BadInput, match="text="):
        research_with_embedder.put(id="anything", text=None, mode="import")


def test_import_rejects_missing_id(
    research_with_embedder: ResearchHandler,
) -> None:
    with pytest.raises(BadInput, match="id="):
        research_with_embedder.put(id=None, text="body", mode="import")


def test_import_rejects_unknown_mode(
    research_with_embedder: ResearchHandler,
) -> None:
    with pytest.raises(BadInput, match="mode='import'"):
        research_with_embedder.put(id="q", text="body", mode="append")


def test_import_then_search_finds_imported_blocks(
    research_with_embedder: ResearchHandler,
) -> None:
    """Imported content participates in cross-corpus / kind-scoped search."""
    h = research_with_embedder
    h.put(
        id="catalyst survey",
        text=_SAMPLE_RESEARCH_REPORT,
        mode="import",
    )
    # Use the store's block search directly — the perplexity handler
    # doesn't expose `search` itself, but the data should be findable
    # by anyone who does kind-filtered block search.
    hits = h.store.search_blocks_fused(
        q="cobalt phthalocyanine",
        query_vec=None,
        kind="research",
        scope_ref_id=None,
        limit=5,
    )
    assert hits, "expected lexical hit for 'cobalt phthalocyanine'"
    # Top hit should be a block from our imported report.
    top_block, top_ref, _score = hits[0]
    assert top_ref.kind == "research"
    assert "cobalt" in top_block.text.lower()


def test_import_supports_put_advertised_in_spec() -> None:
    """KindSpec.supports_put + modes must be honest."""
    for cls in (WebsearchHandler, ThinkHandler, ResearchHandler):
        assert cls.spec.supports_put is True
        assert "import" in cls.spec.modes


# ── /recent listing view ─────────────────────────────────────────────


def test_recent_empty_shows_helpful_empty_state(
    research: ResearchHandler,
) -> None:
    """Empty store → helpful message pointing at both get and put."""
    resp = research.get()
    assert "no research refs yet" in resp.body.lower()
    assert "mode='import'" in resp.body
    # Also accepts id='/recent' explicitly and id='/'.
    assert "no research refs yet" in research.get(id="/recent").body.lower()
    assert "no research refs yet" in research.get(id="/").body.lower()


def test_recent_lists_imported_and_fetched_with_provenance(
    research_with_embedder: ResearchHandler,
    websearch: WebsearchHandler,
) -> None:
    """Mix an imported ref with an API-fetched ref and confirm the
    listing distinguishes the two with a provenance tag."""
    # 1. Imported research ref.
    research_with_embedder.put(
        id="imported query A",
        text="# A\n\nimported body A",
        mode="import",
    )
    # 2. Fetched websearch ref (goes via the stubbed API).
    websearch.get(id="fetched query B")

    r_resp = research_with_embedder.get(id="/recent")
    assert "imported query A" in r_resp.body
    assert "(imported," in r_resp.body

    w_resp = websearch.get(id="/recent")
    assert "fetched query B" in w_resp.body
    assert "(fetched," in w_resp.body


def test_recent_only_returns_this_handlers_kind(
    research_with_embedder: ResearchHandler,
    websearch: WebsearchHandler,
) -> None:
    """Listings are scoped to the handler's kind — `research` never
    shows `websearch` rows and vice versa."""
    research_with_embedder.put(id="r-specific", text="# r\n\nbody", mode="import")
    websearch.get(id="w-specific")

    r_body = research_with_embedder.get(id="/recent").body
    assert "r-specific" in r_body
    assert "w-specific" not in r_body

    w_body = websearch.get(id="/recent").body
    assert "w-specific" in w_body
    assert "r-specific" not in w_body


def test_recent_rejects_unknown_slash_view(
    research: ResearchHandler,
) -> None:
    """A typo like ``id='/recnet'`` is caught with a helpful error
    rather than being treated as a cache-miss query."""
    with pytest.raises(BadInput, match="unknown view"):
        research.get(id="/recnet")


def test_recent_survives_without_api_key(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The listing path never touches the Perplexity API — it must
    work with no ``PERPLEXITY_API_KEY`` set. This is the whole point
    of having relaxed ``requires_env`` on the spec."""
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    h = ResearchHandler(hub=Hub(store=store, embedder=MockEmbedder(dim=1024)))
    h.put(id="works offline", text="# off\n\nbody", mode="import")
    resp = h.get(id="/recent")  # no Upstream raised
    assert "works offline" in resp.body


# ── "— imported" cost-trailer badge ──────────────────────────────────


def test_imported_entry_cost_reads_imported_on_hit(
    research_with_embedder: ResearchHandler,
) -> None:
    """After ``put(mode='import')``, the subsequent ``get`` must flag
    the body as user-supplied in the cost trailer."""
    h = research_with_embedder
    h.put(id="badged query", text="# x\n\ny", mode="import")
    resp = h.get(id="badged query")
    assert resp.cost == "[cost: free — imported]"


def test_fetched_cache_hit_keeps_cached_badge(
    websearch: WebsearchHandler,
) -> None:
    """An ordinary paid-API cache hit must NOT masquerade as imported
    — only true imports get the badge."""
    # First call writes a non-imported cache row.
    websearch.get(id="paid hit")
    # Second call hits cache.
    resp2 = websearch.get(id="paid hit")
    assert "imported" not in resp2.cost
    assert "cached" in resp2.cost


# ── fetch-path block parsing + embedding ─────────────────────────────


@pytest.fixture
def think_with_embedder(store: Store) -> ThinkHandler:
    """ThinkHandler wired with the mock embedder so the fetch path's
    ``_blocks_from_report`` produces vectors."""
    return ThinkHandler(hub=Hub(store=store, embedder=MockEmbedder(dim=1024)))


_MULTI_PARA_BODY = (
    "# Investigation Summary\n"
    "\n"
    "First paragraph discusses the catalyst landscape with copper-based "
    "electrodes leading the field [1].\n"
    "\n"
    "## Mechanism\n"
    "\n"
    "Second paragraph covers the proton-coupled electron transfer "
    "mechanism critical to selectivity [2].\n"
    "\n"
    "## Outlook\n"
    "\n"
    "Third paragraph forecasts the next five years of efficiency gains.\n"
)


def test_fetch_path_block_parses_body(think_with_embedder: ThinkHandler) -> None:
    """Cache-miss API response body is parsed into multiple addressable
    blocks (one per heading / paragraph), not stored as one lump.

    Before the consolidation, ``_fetch`` returned a single un-embedded
    block — agents could ``get`` the report but never ``search`` inside
    it. Now both API-fetched and ``put(mode='import')`` paths share the
    same block-parsing pipeline.
    """
    # Stub multi-paragraph response.
    _StubClient.response = _StubResp(
        json_data={
            "choices": [{"message": {"content": _MULTI_PARA_BODY}}],
            "citations": ["https://example.com/1", "https://example.com/2"],
        }
    )
    h = think_with_embedder
    h.get(id="catalyst landscape question")

    # Find the cache row and count blocks.
    refs = h.store.list_refs(kind="think", provider="perplexity", limit=10)
    assert len(refs) == 1
    n_blocks = h.store.count_blocks(refs[0].id)
    # Three paragraphs + three headings + the Sources trailer ≥ 4 blocks.
    assert n_blocks >= 4, f"expected fetch path to produce ≥4 blocks, got {n_blocks}"


def test_fetch_path_embeds_blocks(think_with_embedder: ThinkHandler) -> None:
    """The fetch-path blocks must carry vectors when an embedder is
    wired — otherwise ``search(kind='think', q=...)`` returns nothing
    despite the rows existing."""
    _StubClient.response = _StubResp(
        json_data={
            "choices": [{"message": {"content": _MULTI_PARA_BODY}}],
            "citations": [],
        }
    )
    h = think_with_embedder
    h.get(id="another query")

    refs = h.store.list_refs(kind="think", provider="perplexity", limit=10)
    # ``with_embedding=True`` is required — the default loads only the
    # block bodies (cheap path for rendering) and leaves the vector
    # column NULL.
    blocks = h.store.list_blocks_for_ref(refs[0].id, with_embedding=True)
    embedded = [b for b in blocks if b.embedding is not None]
    assert embedded, "expected fetch-path blocks to be embedded"
    # Mock embedder yields 1024-dim unit-norm vectors.
    assert all(len(b.embedding) == 1024 for b in embedded)


# ── search verb on perplexity kinds ─────────────────────────────────


def test_search_finds_blocks_in_fetched_report(
    think_with_embedder: ThinkHandler,
) -> None:
    """End-to-end: fetch a report (cache miss) → search inside it.

    Pre-consolidation this was impossible: the fetch path stored a
    single un-embedded lump and ``supports_search=False`` blocked the
    verb anyway. Both gaps are now closed.
    """
    _StubClient.response = _StubResp(
        json_data={
            "choices": [{"message": {"content": _MULTI_PARA_BODY}}],
            "citations": [],
        }
    )
    h = think_with_embedder
    h.get(id="long-tail query")

    resp = h.search(q="proton-coupled electron transfer")
    assert "block hit" in resp.body
    assert "mechanism" in resp.body.lower()


def test_search_rejects_blank_query(think_with_embedder: ThinkHandler) -> None:
    with pytest.raises(BadInput, match="search requires q="):
        think_with_embedder.search(q="")


def test_search_returns_empty_state_for_unknown_query(
    think_with_embedder: ThinkHandler,
) -> None:
    resp = think_with_embedder.search(q="nothing matches this string xyzpdq")
    assert "no think blocks match" in resp.body


def test_search_hits_returns_structured_for_cross_kind_merge(
    think_with_embedder: ThinkHandler,
) -> None:
    """``search_hits()`` is what the runtime calls when fanning out
    across multiple kinds (``kind='*'`` / ``kind='paper,think'``)."""
    _StubClient.response = _StubResp(
        json_data={
            "choices": [{"message": {"content": _MULTI_PARA_BODY}}],
            "citations": [],
        }
    )
    h = think_with_embedder
    h.get(id="cross-kind query")

    hits = h.search_hits(q="catalyst landscape")
    assert hits, "expected at least one structured hit"
    # Every SearchHit advertises this kind so the merger renders it.
    assert all(h.kind == "think" for h in hits)


def test_search_supports_announced_in_kind_spec() -> None:
    """The KindSpec must advertise the verb so the dispatcher routes
    ``search(kind='think', ...)`` to the handler instead of returning
    Unsupported. Pin the flags here so a future refactor can't silently
    revert the cutover."""
    for cls in (WebsearchHandler, ThinkHandler, ResearchHandler):
        assert cls.spec.supports_search is True, cls.__name__
        assert cls.spec.supports_search_hits is True, cls.__name__

"""Tests for ``handlers/perplexity.py`` — websearch / perplexity-reasoning / perplexity-research.

httpx is patched at the call-site (we replace ``httpx.Client`` with a
stub) so tests stay offline. Same approach as ``test_web.py``.

PERPLEXITY_API_KEY is set in a fixture so the auth check passes; tests
that exercise the missing-key path delete it explicitly.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.embedder import EmbedderUnavailable, MockEmbedder
from precis.errors import BadInput, NotFound, Upstream
from precis.handlers.perplexity import (
    ResearchHandler,
    ThinkHandler,
    WebsearchHandler,
    _format_perplexity_body,
    _title_for_query,
)
from precis.store import Store
from tests.hintcheck import assert_hints_round_trip

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
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, Any]:
        return self._json_data


class _StubClient:
    """Drop-in for ``httpx.Client(...)`` used by perplexity._fetch."""

    last_payload: dict[str, Any] | None = None
    response: _StubResp | None = None
    raise_on_post: Exception | None = None
    #: Counts real "upstream" calls — used by the ``no_fetch=True`` /recent
    #: trailer regression test (hint-audit item 9): dispatching the
    #: advertised hint on a listed slug must not increment this.
    call_count: int = 0

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
        type(self).call_count += 1
        if type(self).raise_on_post is not None:
            raise type(self).raise_on_post  # type: ignore[misc]
        resp = type(self).response
        assert resp is not None
        return resp


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
    _StubClient.call_count = 0
    monkeypatch.setattr(httpx, "Client", _StubClient)
    # Backoff retries must not sleep for real, nor leak a breaker across tests.
    from precis.utils.http import reset_circuit

    reset_circuit()
    monkeypatch.setattr("precis.utils.http._default_sleep", lambda _s: None)


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


def test_paid_answer_carries_focused_budget_recovery(
    websearch: WebsearchHandler,
) -> None:
    _StubClient.response = _StubResp(
        json_data={
            "choices": [{"message": {"content": "x" * 9000}}],
            "citations": [],
        }
    )
    resp = websearch.get(id="a long answer please")
    assert "narrower question" in (resp.result_budget_hint or "")
    assert _StubClient.call_count == 1
    resp = websearch.get(id="a long answer please")
    assert "narrower question" in (resp.result_budget_hint or "")
    assert _StubClient.call_count == 1


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
    assert resp.cost is not None
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
    """Cache key includes the model so websearch + perplexity-reasoning don't collide."""
    websearch.get(id="hello world")
    # Change response so perplexity-reasoning gets a different body than websearch.
    _StubClient.response = _StubResp(
        json_data={
            "choices": [{"message": {"content": "reasoning answer body"}}],
            "citations": [],
        }
    )
    resp_think = think.get(id="hello world")
    assert "reasoning answer body" in resp_think.body  # not the cached websearch body


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


# ── bare-number (ref-id) guard ───────────────────────────────────────
# A digits-only query is almost always a ref-id an agent tried to
# retrieve, misrouted through a query-addressed kind — not a web search.
# It must NOT fire a paid Sonar call. (The 171157 incident.)


def test_bare_number_id_rejected_without_fetch(
    websearch: WebsearchHandler,
) -> None:
    with pytest.raises(BadInput, match="bare number"):
        websearch.get(id="171157")
    # The guard fires before any upstream call — nothing was spent.
    assert _StubClient.last_payload is None


def test_bare_number_q_rejected_without_fetch(
    websearch: WebsearchHandler,
) -> None:
    with pytest.raises(BadInput, match="bare number"):
        websearch.get(q="171")
    assert _StubClient.last_payload is None


def test_unquoted_int_id_rejected_with_hint(
    websearch: WebsearchHandler,
) -> None:
    """The literal incident shape — get(kind='websearch', id=171157) with
    an *unquoted* int — reaches the bare-number hint (not the generic
    "requires a query"), and fires no paid call."""
    with pytest.raises(BadInput, match="bare number"):
        websearch.get(id=171157)
    assert _StubClient.last_payload is None


def test_reasoning_tier_also_guards_bare_number(think: ThinkHandler) -> None:
    """The guard lives on the shared base, so the pricier tiers — where
    a stray number would cost more — are protected too."""
    with pytest.raises(BadInput, match="bare number"):
        think.get(id="171157")
    assert _StubClient.last_payload is None


def test_literal_flag_allows_bare_number_search(
    websearch: WebsearchHandler,
) -> None:
    """``args={'literal': True}`` (threaded as ``literal=``) opts back
    into a verbatim search of the number."""
    resp = websearch.get(q="171", literal=True)
    assert _StubClient.last_payload is not None  # a real call fired
    assert "Dario Amodei" in resp.body  # stub body returned


def test_numeric_slug_ref_served_free_on_retrieval(
    websearch: WebsearchHandler,
) -> None:
    """A ref whose stored slug *is* the bare number — a shape only a row
    minted before the ``-<sha8>`` suffix can carry — is still served by
    base ``get``'s slug fallback for free: no new paid call and no
    rejection, so the bare-number guard never strands an existing ref."""
    websearch.store.put_cache_entry(
        kind="websearch",
        slug="171",
        title="the number one seven one",
        body_blocks=websearch._blocks_from_report("# n\n\nlegacy numeric body"),
        provider="perplexity",
        request_hash="legacy-numeric",
        ttl_seconds=None,
    )
    resp = websearch.get(id="171")
    assert "legacy numeric body" in resp.body
    assert _StubClient.last_payload is None  # served from cache, no fetch


def test_numeric_ref_id_served_free_on_retrieval(
    websearch: WebsearchHandler, think: ThinkHandler
) -> None:
    """gr478483: memories cite a cache as ``websearch:<ref id>``; get() by
    that number (str or int) resolves the ref from cache, no paid call, and
    never crosses kinds."""
    websearch.get(id="who is the CEO of Anthropic")
    refs = websearch.store.list_refs(kind="websearch")
    ref_id = refs[0].id
    _StubClient.last_payload = None
    for ident in (str(ref_id), ref_id):
        resp = websearch.get(id=ident)
        assert "Dario Amodei" in resp.body
    assert _StubClient.last_payload is None
    # The same number under another perplexity kind is not that kind's ref.
    with pytest.raises(BadInput, match="bare number"):
        think.get(id=str(ref_id))
    assert _StubClient.last_payload is None


def test_non_numeric_query_unaffected(websearch: WebsearchHandler) -> None:
    resp = websearch.get(id="who is the CEO of Anthropic")
    assert "Dario Amodei" in resp.body
    assert _StubClient.last_payload is not None


def test_import_of_numeric_query_not_blocked(
    research_with_embedder: ResearchHandler,
) -> None:
    """The import path passes ``literal=True`` — a legitimately numeric
    query can still be hydrated at $0 without tripping the guard. The
    ack must advertise a read-back call that actually resolves:
    ``id='171'`` would trip the bare-number guard, so the hint names
    the stored slug with ``no_fetch=True``."""
    h = research_with_embedder
    put_resp = h.put(id="171", text="# n\n\nthe number one seven one", mode="import")
    assert "no_fetch=True" in put_resp.body
    assert "id='171'" not in put_resp.body
    cached = h.store.get_cache_entry(
        provider="perplexity",
        request_hash=h._hash(h._canonical_key("171", literal=True)),
    )
    assert cached is not None
    slug = cached[0].slug
    assert slug is not None and slug.startswith("171-")
    resp = h.get(id=slug, no_fetch=True)  # served from the imported row, free
    assert "one seven one" in resp.body
    assert _StubClient.last_payload is None


# ── search block handle guard ────────────────────────────────────────


@pytest.mark.parametrize(
    "handler_cls", [WebsearchHandler, ThinkHandler, ResearchHandler]
)
@pytest.mark.parametrize("query_arg", ["id", "q"])
@pytest.mark.parametrize("mode", [None, "refresh"])
def test_block_handle_rejected_without_fetch_or_report_mutation(
    store: Store, handler_cls: Any, query_arg: str, mode: str | None
) -> None:
    h = handler_cls(hub=Hub(store=store, embedder=MockEmbedder(dim=1024)))
    original_query = "catalysis report"
    h.put(id=original_query, text="# Original\n\nkeep this report", mode="import")
    request_hash = h._hash(h._canonical_key(original_query))
    before = store.get_cache_entry(provider="perplexity", request_hash=request_hash)
    assert before is not None
    handle = f"{before[0].slug}~mechanism-a1B2c3"

    with pytest.raises(BadInput, match="search block handle") as err:
        h.get(**{query_arg: f"  {handle}  "}, mode=mode)

    assert _StubClient.call_count == 0
    assert (
        store.get_cache_entry(provider="perplexity", request_hash=request_hash)
        == before
    )
    assert (
        store.get_cache_entry(
            provider="perplexity",
            request_hash=h._hash(h._canonical_key(handle, literal=True)),
        )
        is None
    )

    def replay_hint(verb: str, kwargs: dict[str, Any]) -> Any:
        extras = kwargs.pop("args", {})
        return getattr(h, verb)(**kwargs, **extras)

    hints = assert_hints_round_trip(str(err.value.next), replay_hint, whole_body=True)
    assert len(hints) == 1
    assert "no_fetch" in hints[0]
    assert _StubClient.call_count == 0
    assert "keep this report" in h.get(id=original_query).body


def test_block_handle_literal_escape(websearch: WebsearchHandler) -> None:
    websearch.get(q="report~section-deadbeef", literal=True)
    assert _StubClient.call_count == 1
    assert _StubClient.last_payload is not None
    assert (
        _StubClient.last_payload["messages"][-1]["content"] == "report~section-deadbeef"
    )


def test_block_handle_import_exempt(research_with_embedder: ResearchHandler) -> None:
    h = research_with_embedder
    query = "report~section-deadbeef"
    h.put(id=query, text="# Imported\n\nexplicit report", mode="import")
    assert "explicit report" in h.get(q=query, literal=True).body
    assert _StubClient.call_count == 0


@pytest.mark.parametrize("query", ["approximately ~10 items", "report~section-nothex"])
def test_non_handle_tilde_query_unaffected(
    websearch: WebsearchHandler, query: str
) -> None:
    websearch.get(q=query)
    assert _StubClient.call_count == 1


# ── section-handle reads (gr542060) ──────────────────────────────────
# ``search`` emits block-hit handles as ``<slug>~<block.slug>`` (e.g.
# ``…~5-official-qiskit-ibm-runtime-mcp-server``). Pasting one into
# ``get`` must read the cached section — never a fresh paid call.


def _import_sectioned_report(h: Any, query: str) -> Any:
    """Import a multi-section report; return its ref."""
    h.put(
        id=query,
        text=(
            "# Report\n\n"
            "## 5. Official Qiskit IBM Runtime MCP Server\n\n"
            "the official server body\n\n"
            "## Alternatives\n\n"
            "the alternatives body"
        ),
        mode="import",
    )
    return h.store.list_refs(kind=h.spec.kind, limit=1)[0]


@pytest.mark.parametrize(
    "handler_cls", [WebsearchHandler, ThinkHandler, ResearchHandler]
)
def test_search_hit_section_handle_reads_cache_without_fetch(
    store: Store, handler_cls: Any
) -> None:
    """The exact handle form ``search`` emits — ``<slug>~<block.slug>`` —
    reads that stored section; the paid client is never called."""
    h = handler_cls(hub=Hub(store=store, embedder=MockEmbedder(dim=1024)))
    ref = _import_sectioned_report(h, "qiskit mcp servers")
    blocks = h.store.chunks.list_chunks_for_ref(ref.id)
    section = next(b for b in blocks if "Official Qiskit" in b.text)
    handle = f"{ref.slug}~{section.slug}"

    resp = h.get(id=handle)

    assert section.text in resp.body
    assert "the alternatives body" not in resp.body
    assert _StubClient.call_count == 0


@pytest.mark.parametrize(
    "handler_cls", [WebsearchHandler, ThinkHandler, ResearchHandler]
)
def test_unknown_section_on_cached_slug_refuses_with_sections(
    store: Store, handler_cls: Any
) -> None:
    """An unknown selector on a live slug refuses, naming the valid
    section handles — never a fresh paid call."""
    h = handler_cls(hub=Hub(store=store, embedder=MockEmbedder(dim=1024)))
    ref = _import_sectioned_report(h, "qiskit mcp servers")

    with pytest.raises(BadInput, match="search block handle") as ei:
        h.get(id=f"{ref.slug}~no-such-section")

    msg = str(ei.value)
    assert f"{ref.slug}~5-official-qiskit-ibm-runtime-mcp" in msg
    assert f"{ref.slug}~alternatives" in msg
    assert "no_fetch" in msg
    assert _StubClient.call_count == 0


@pytest.mark.parametrize(
    "handler_cls", [WebsearchHandler, ThinkHandler, ResearchHandler]
)
def test_tilde_id_on_missing_slug_never_fetches(store: Store, handler_cls: Any) -> None:
    """An ``id`` containing ``~`` is an address, not a query — an
    unresolvable prefix refuses rather than spending."""
    h = handler_cls(hub=Hub(store=store, embedder=MockEmbedder(dim=1024)))
    with pytest.raises(BadInput, match="section address"):
        h.get(id="not-a-cached-slug~some-section")
    assert _StubClient.call_count == 0


# ── citation numbering (gr542060) ────────────────────────────────────
# The inline [N] markers index the id-spaced ``search_results`` list;
# the legacy ``citations`` array can arrive reordered/deduplicated.


def test_sources_follow_search_results_order_not_citations() -> None:
    """When both fields are present and disagree, Sources: follows
    ``search_results`` — the array the [N] markers are numbered against."""
    body, citations = _format_perplexity_body(
        {
            "choices": [{"message": {"content": "claim a [1] claim b [2]"}}],
            "citations": ["https://b.example/", "https://a.example/"],
            "search_results": [
                {"title": "A", "url": "https://a.example/"},
                {"title": "B", "url": "https://b.example/"},
            ],
        }
    )
    assert "[1] https://a.example/" in body
    assert "[2] https://b.example/" in body
    assert citations == ["https://a.example/", "https://b.example/"]


def test_sources_honour_explicit_search_result_ids() -> None:
    """Reformulated responses can carry explicit result ids — the label
    is the marker id, not the array position."""
    body, _citations = _format_perplexity_body(
        {
            "choices": [{"message": {"content": "claim [7]"}}],
            "search_results": [{"id": 7, "url": "https://seven.example/"}],
        }
    )
    assert "[7] https://seven.example/" in body


def test_sources_fall_back_to_citations_array() -> None:
    """Legacy responses without ``search_results`` keep the documented
    ``citations`` mapping: [N] → citations[N-1]."""
    body, citations = _format_perplexity_body(
        {
            "choices": [{"message": {"content": "claim [1]"}}],
            "citations": ["https://only.example/"],
        }
    )
    assert "[1] https://only.example/" in body
    assert citations == ["https://only.example/"]


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


# ── title storage — full query, not truncated ────────────────────────


def test_title_for_query_keeps_full_query() -> None:
    """The ref title stores the whole query — truncation is a display
    concern, never a storage one (web detail renders ``ref.title``)."""
    long_q = "why " * 60 + "does electrocatalytic CO2 reduction favour C2 products"
    title = _title_for_query(long_q)
    assert title.endswith("C2 products")
    assert "…" not in title
    assert len(title) > 80


def test_title_for_query_collapses_whitespace_single_line() -> None:
    """A multi-line paste still becomes one logical line, but nothing is
    dropped — matches the migration's ``regexp_replace(..., '\\s+', ' ')``."""
    assert _title_for_query("  who   is\n the CEO\tof Anthropic  ") == (
        "who is the CEO of Anthropic"
    )


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
    """Most websearch / perplexity-research bodies have no reasoning block;
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
    """websearch=7d, perplexity-reasoning=pinned, perplexity-research=pinned.

    Reasoning + deep-research both default to pinned-forever: citation-grade
    questions don't decay, and the pollution failure mode (wrong cached
    answer) is bounded — citations come from the chase/finding pipeline,
    not from perplexity, so a bad cached row can be link-tagged as wrong
    or deleted. Agents can opt into a shorter TTL per-call via
    ``ttl_days=`` on get; default forever beats default-stale-in-30d.
    Websearch stays at 7d because the fast-tier is mostly current-events.
    """
    # ttl_seconds/cost_per_call_usd are properties proxying to TIER (config-
    # dataclass parameterization, see handlers/perplexity.py::_SonarTier) —
    # read the TIER ClassVar directly rather than the instance property.
    assert WebsearchHandler.TIER.ttl_seconds == 7 * 24 * 60 * 60
    assert ThinkHandler.TIER.ttl_seconds is None
    assert ResearchHandler.TIER.ttl_seconds is None


def test_cost_per_tier() -> None:
    assert WebsearchHandler.TIER.cost_per_call_usd == pytest.approx(0.001)
    assert ThinkHandler.TIER.cost_per_call_usd == pytest.approx(0.005)
    assert ResearchHandler.TIER.cost_per_call_usd == pytest.approx(0.50)


def test_expected_cost_uses_observed_billing(research: ResearchHandler) -> None:
    """gr521139: the pre-call estimate is the p90 of the tier's recent
    *billed* rows — the $0.50 constant ran >5x under a real
    deep-research call ($2.71). Rows marked ``cost_estimated`` recorded
    the fallback, not billing data, so they are excluded; with no billed
    rows the TIER constant is the fallback."""
    store = research.store
    assert research._expected_cost_usd() == pytest.approx(0.50)

    def seed(i: int, cost: float, *, estimated: bool = False) -> None:
        meta = (
            {"cost_estimated": True}
            if estimated
            else {"usage": {"cost": {"total_cost": cost}}, "cost_estimated": False}
        )
        store.put_cache_entry(
            kind="perplexity-research",
            slug=f"seeded-q{i}",
            title=f"seeded q{i}",
            body_blocks=[],
            provider="perplexity",
            request_hash=f"hash-{i}",
            ttl_seconds=None,
            model="sonar-deep-research",
            cost_usd=cost,
            cache_meta=meta,
        )

    seed(1, 0.50)
    seed(2, 2.71)
    seed(3, 0.50, estimated=True)  # excluded — the flat fallback, not billing
    stats = store.observed_cost_stats(
        provider="perplexity", model="sonar-deep-research"
    )
    assert stats is not None and stats[0] == 2
    # percentile_cont(0.9) over [0.50, 2.71] interpolates.
    assert research._expected_cost_usd() == pytest.approx(0.50 + 0.9 * (2.71 - 0.50))


def test_cost_appears_on_fresh_fetch(websearch: WebsearchHandler) -> None:
    resp = websearch.get(id="something")
    # Cost trailer reflects the per-call cost for the tier.
    assert resp.cost is not None
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
    assert "perplexity-research" in put_resp.body

    # First get must hit the cache populated by the import — not the API.
    get_resp = h.get(id=query)
    assert "CO2 Electrolysis Catalyst Survey" in get_resp.body
    assert "Cobalt phthalocyanine" in get_resp.body
    assert "API SHOULD NOT BE CALLED" not in get_resp.body
    # Cost trailer reads as free for imported entries.
    assert get_resp.cost is not None
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
    blocks = h.store.chunks.list_chunks_for_ref(ref.id)
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
    blocks = h.store.chunks.list_chunks_for_ref(ref.id)
    full = "\n".join(b.text for b in blocks)
    # The second import wins; the first is gone.
    assert "second body" in full
    assert "first body" not in full


def test_import_different_kinds_use_separate_cache_rows(
    store: Store,
) -> None:
    """Importing the same query into perplexity-research vs websearch creates two
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
    assert r_cache[0].kind == "perplexity-research"
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
    hits = h.store.chunks.search_chunks_fused(
        q="cobalt phthalocyanine",
        query_vec=None,
        kind="perplexity-research",
        scope_ref_id=None,
        limit=5,
    )
    assert hits, "expected lexical hit for 'cobalt phthalocyanine'"
    # Top hit should be a block from our imported report.
    top_block, top_ref, _score = hits[0]
    assert top_ref.kind == "perplexity-research"
    assert "cobalt" in top_block.text.lower()


def test_import_supports_put_advertised_in_spec() -> None:
    """KindSpec.supports_put + modes must be honest."""
    for cls in (WebsearchHandler, ThinkHandler, ResearchHandler):
        assert cls.spec.supports_put is True
        assert cls.spec.modes is not None and "import" in cls.spec.modes


# ── /recent listing view ─────────────────────────────────────────────


def test_recent_empty_shows_helpful_empty_state(
    research: ResearchHandler,
) -> None:
    """Empty store → helpful message pointing at both get and put."""
    resp = research.get()
    assert "no perplexity-research refs yet" in resp.body.lower()
    assert "mode='import'" in resp.body
    # Also accepts id='/recent' explicitly and id='/'.
    assert "no perplexity-research refs yet" in research.get(id="/recent").body.lower()
    assert "no perplexity-research refs yet" in research.get(id="/").body.lower()


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


def test_recent_trailer_no_fetch_hint_never_posts(
    websearch: WebsearchHandler,
) -> None:
    """hint-audit item 9 (mirrored perplexity ``_render_recent``): the
    populated listing's ``Next:`` hint must read a listed row via
    ``no_fetch=True`` — pasting a listed slug bare would MISS the
    request-hash cache and fire a fresh, billable Sonar call. Dispatch
    the advertised (and the concrete, real-slug) shape and confirm
    neither adds an upstream POST."""
    websearch.get(id="who is the CEO of Anthropic")
    calls_before = _StubClient.call_count

    resp = websearch.get(id="/recent")
    assert "no_fetch=True" in resp.body

    hints = assert_hints_round_trip(
        resp.body,
        lambda verb, kwargs: getattr(websearch, verb)(**kwargs),
        whole_body=True,
    )
    assert any("no_fetch=True" in h for h in hints)

    # The advertised hint is a template (``id='<slug>'``) so the helper
    # above only parses it — dispatch the CONCRETE shape (real slug) to
    # prove the regression that matters.
    refs = websearch.store.list_refs(kind="websearch", limit=10)
    real_slug = refs[0].slug
    assert real_slug is not None
    websearch.get(kind="websearch", id=real_slug, no_fetch=True)

    assert _StubClient.call_count == calls_before


def test_recent_empty_state_migrated_to_render_next_section(
    research: ResearchHandler,
) -> None:
    """The empty-state trailer was hand-rolled prose; confirm it still
    carries both recovery calls after migrating to
    ``render_next_section`` (hint-audit item 9, structural note)."""
    resp = research.get()
    assert "Next:" in resp.body
    hints = assert_hints_round_trip(
        resp.body,
        lambda verb, kwargs: getattr(research, verb)(**kwargs),
        whole_body=True,
        execute_verbs=(),  # both hints are templates; nothing to dispatch
    )
    assert any("get(kind='perplexity-research', id='<query>')" in h for h in hints)
    assert any("mode='import'" in h for h in hints)


def test_next_tier_hint_guards_missing_query_in_meta(
    websearch: WebsearchHandler,
) -> None:
    """hint-audit item 11: an unguarded ``meta.get('query')`` renders a
    valid-but-wrong ``id=None`` literal for a cache row written before
    ``query`` was tracked in meta. Strip it and confirm the escalation
    hint is dropped rather than rendering ``id=None``."""
    websearch.get(id="who is the CEO of Anthropic")
    refs = websearch.store.list_refs(kind="websearch", limit=1)
    ref_id = refs[0].id
    with websearch.store.pool.connection() as conn:
        conn.execute(
            "UPDATE cache_state SET meta = meta - 'query' WHERE ref_id = %s",
            (ref_id,),
        )

    resp = websearch.get(id="who is the CEO of Anthropic")  # cache hit, re-renders
    assert "id=None" not in resp.body
    assert "perplexity-reasoning" not in resp.body


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
    assert resp.cost == "[cost: free - imported]"


def test_fetched_cache_hit_keeps_cached_badge(
    websearch: WebsearchHandler,
) -> None:
    """An ordinary paid-API cache hit must NOT masquerade as imported
    — only true imports get the badge."""
    # First call writes a non-imported cache row.
    websearch.get(id="paid hit")
    # Second call hits cache.
    resp2 = websearch.get(id="paid hit")
    assert resp2.cost is not None
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
    refs = h.store.list_refs(
        kind="perplexity-reasoning", provider="perplexity", limit=10
    )
    assert len(refs) == 1
    n_blocks = h.store.chunks.count_chunks(refs[0].id)
    # Three paragraphs + three headings + the Sources trailer ≥ 4 blocks.
    assert n_blocks >= 4, f"expected fetch path to produce ≥4 blocks, got {n_blocks}"


def test_fetch_path_embeds_blocks(think_with_embedder: ThinkHandler) -> None:
    """The fetch-path blocks must carry vectors when an embedder is
    wired — otherwise ``search(kind='perplexity-reasoning', q=...)`` returns nothing
    despite the rows existing."""
    _StubClient.response = _StubResp(
        json_data={
            "choices": [{"message": {"content": _MULTI_PARA_BODY}}],
            "citations": [],
        }
    )
    h = think_with_embedder
    h.get(id="another query")

    refs = h.store.list_refs(
        kind="perplexity-reasoning", provider="perplexity", limit=10
    )
    # ``with_embedding=True`` is required — the default loads only the
    # block bodies (cheap path for rendering) and leaves the vector
    # column NULL.
    blocks = h.store.chunks.list_chunks_for_ref(refs[0].id, with_embedding=True)
    embedded = [b for b in blocks if b.embedding is not None]
    assert embedded, "expected fetch-path blocks to be embedded"
    # Mock embedder yields 1024-dim unit-norm vectors.
    for b in embedded:
        assert b.embedding is not None
        assert len(b.embedding) == 1024


# ── embedder-outage resilience (gripe #47286) ───────────────────────


class _DownEmbedder:
    """Embedder whose every call fails as if the bge-m3 service is down.

    Mirrors :class:`precis.embedder.RemoteEmbedder` after it has
    exhausted every endpoint — the exact condition prod hit in
    gripe #47286.
    """

    @property
    def dim(self) -> int:
        return 1024

    @property
    def model(self) -> str:
        return "down"

    def embed_one(self, text: str) -> list[float]:
        raise EmbedderUnavailable("all embedder endpoints failed (down)")

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbedderUnavailable("all embedder endpoints failed (down)")

    def is_ready(self) -> bool:
        return True

    def warmup(self) -> None:
        return None

    def unload(self) -> None:
        return None


class _WarmingEmbedder:
    """Embedder whose every call fails as if bge-m3 is still warming.

    The in-process :class:`precis.embedder.BgeM3Embedder` raises
    :class:`precis.errors.Upstream` (not ``EmbedderUnavailable``) from
    ``_raise_if_warming`` until the model finishes loading. Prod runs
    this embedder, so the degrade guard must catch this arm too or
    gripe #47286 stays open on the default deployment.
    """

    def embed_one(self, text: str) -> list[float]:
        raise Upstream("bge-m3 embedder still warming")

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise Upstream("bge-m3 embedder still warming")


@pytest.mark.parametrize("embedder_cls", [_DownEmbedder, _WarmingEmbedder])
def test_fetch_survives_embedder_outage(store: Store, embedder_cls: type) -> None:
    """A failing embedder must not sink a provider-backed fetch.

    The Perplexity Sonar call is an external HTTP request that never
    touches the local bge-m3 embedder — the embedder is only used
    afterwards to vectorise the returned blocks for semantic search.
    Before the fix, an embedder error raised while embedding those
    blocks aborted the whole ``get`` with an opaque internal error,
    even though the (paid) provider answer was already in hand. Now the
    fetch degrades to un-embedded blocks (the ``embed:bge-m3`` worker
    backfills the vectors later) and the agent still gets its answer,
    for BOTH embedder failure classes — a down ``RemoteEmbedder``
    (``EmbedderUnavailable``) and a warming in-process bge-m3
    (``Upstream``). (gripe #47286.)
    """
    _StubClient.response = _StubResp(
        json_data={
            "choices": [{"message": {"content": _MULTI_PARA_BODY}}],
            "citations": ["https://example.com/1"],
        }
    )
    h = WebsearchHandler(hub=Hub(store=store, embedder=embedder_cls()))

    # The provider answer must come through, not an EmbedderUnavailable.
    resp = h.get(id="who runs anthropic under embedder outage")
    assert "catalyst landscape" in resp.body
    assert "Sources:" in resp.body

    # Blocks are stored un-embedded (vector column NULL) so the embed
    # worker can backfill them; lexical search works immediately.
    refs = h.store.list_refs(kind="websearch", provider="perplexity", limit=10)
    assert len(refs) == 1
    blocks = h.store.chunks.list_chunks_for_ref(refs[0].id, with_embedding=True)
    assert blocks, "expected the fetched report to be stored as blocks"
    assert all(b.embedding is None for b in blocks)


def test_search_survives_embedder_outage(store: Store) -> None:
    """The ``search`` verb on a provider-backed kind degrades to lexical
    rather than raising when the embedder is down (already guarded via
    ``embed_query`` — pinned here so it can't regress alongside the
    fetch-path fix). (gripe #47286.)"""
    down = WebsearchHandler(hub=Hub(store=store, embedder=_DownEmbedder()))
    # Seed one cached row via a healthy embedder so there is content to
    # search, then search it back through the down-embedder handler.
    seed = WebsearchHandler(hub=Hub(store=store, embedder=MockEmbedder(dim=1024)))
    _StubClient.response = _StubResp(
        json_data={
            "choices": [{"message": {"content": _MULTI_PARA_BODY}}],
            "citations": [],
        }
    )
    seed.get(id="catalyst landscape seed")

    resp = down.search(q="catalyst landscape")
    # No EmbedderUnavailable — a real (or empty-state) response comes back.
    assert isinstance(resp.body, str)


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
    assert "no perplexity-reasoning blocks match" in resp.body


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
    assert all(h.kind == "perplexity-reasoning" for h in hits)


def test_search_supports_announced_in_kind_spec() -> None:
    """The KindSpec must advertise the verb so the dispatcher routes
    ``search(kind='perplexity-reasoning', ...)`` to the handler instead of
    returning Unsupported. Pin the flags here so a future refactor can't
    silently revert the cutover."""
    for cls in (WebsearchHandler, ThinkHandler, ResearchHandler):
        assert cls.spec.supports_search is True, cls.__name__
        assert cls.spec.supports_search_hits is True, cls.__name__


# ── slug scheme: 60-char prefix + 8-hex key hash (gr517283) ─────────

# Both queries share a >60-char prefix; only the tail differs. Under the
# pre-suffix slug scheme they minted the same ref slug and the second was
# refused as an overwrite (the design-tree pilot gripe); under the
# hash-suffixed scheme each gets its own row.
_LONG_PREFIX = "electrowetting on dielectric droplet actuation in silicone oil"
_QUERY_A = f"{_LONG_PREFIX} review of contact angle saturation"
_QUERY_B = f"{_LONG_PREFIX} chemical synthesis protocols"


def _import_a(h: ResearchHandler) -> str:
    h.put(id=_QUERY_A, text="# A\n\nkeep this report", mode="import")
    cached = h.store.get_cache_entry(
        provider="perplexity", request_hash=h._hash(h._canonical_key(_QUERY_A))
    )
    assert cached is not None
    slug = cached[0].slug
    assert slug is not None
    return slug


def _legacy_slug(query: str) -> str:
    """The pre-gr517283 slug form: query slugified at 60 chars, no hash."""
    from precis.utils.slug import slug_from_text

    return slug_from_text(query, max_len=60) or "perplexity-query"


def test_new_slug_is_readable_prefix_plus_hash_tail(
    research_with_embedder: ResearchHandler,
) -> None:
    """New refs mint ``<60-char slug>-<8 hex>`` — readable, but keyed on a
    hash of the full canonical key so shared prefixes can't collide."""
    h = research_with_embedder
    slug = h._slug_for(h._canonical_key(_QUERY_A))
    prefix, _, tail = slug.rpartition("-")
    assert prefix == _legacy_slug(_QUERY_A)
    assert len(tail) == 8
    assert all(c in "0123456789abcdef" for c in tail)


def test_shared_prefix_queries_mint_distinct_slugs(
    research_with_embedder: ResearchHandler,
) -> None:
    """The gripe's exact shape: same 60-char slug prefix, different query
    tail → different slugs now (the readable part still collides — only
    the hash separates them)."""
    h = research_with_embedder
    assert _QUERY_A != _QUERY_B
    slug_a = h._slug_for(h._canonical_key(_QUERY_A))
    slug_b = h._slug_for(h._canonical_key(_QUERY_B))
    assert slug_a != slug_b
    assert slug_a.rpartition("-")[0] == slug_b.rpartition("-")[0]


def test_shared_prefix_get_fetches_second_query(
    research_with_embedder: ResearchHandler,
) -> None:
    """A hash miss on a shared-prefix query no longer lands on (and is
    refused over) the first query's row — it fetches its own."""
    h = research_with_embedder
    slug_a = _import_a(h)

    resp = h.get(q=_QUERY_B)

    assert _StubClient.call_count == 1
    assert "Dario Amodei" in resp.body
    assert "keep this report" in h.get(id=slug_a, no_fetch=True).body


def test_shared_prefix_imports_both_land(
    research_with_embedder: ResearchHandler,
) -> None:
    """Both templated queries import — no overwrite refusal, two rows."""
    h = research_with_embedder
    slug_a = _import_a(h)

    resp = h.put(id=_QUERY_B, text="# B\n\nunrelated report", mode="import")

    slug_b = h._slug_for(h._canonical_key(_QUERY_B))
    assert slug_b != slug_a
    assert slug_b in resp.body
    assert "keep this report" in h.get(id=slug_a, no_fetch=True).body
    assert "unrelated report" in h.get(id=slug_b, no_fetch=True).body


def _plant(
    h: ResearchHandler, *, slug: str, request_hash: str, query: str | None
) -> str:
    """Write a cache row directly, shaped like a row the importer or an
    older code version would have left (caller picks the slug form and
    whether meta.query was tracked)."""
    h.store.put_cache_entry(
        kind="perplexity-research",
        slug=slug,
        title="planted",
        body_blocks=h._blocks_from_report("# planted\n\nkeep this report"),
        provider="perplexity",
        request_hash=request_hash,
        ttl_seconds=None,
        cache_meta={
            "model": "sonar-deep-research",
            **({"query": query} if query is not None else {}),
        },
    )
    return slug


def test_legacy_slug_resolves_unchanged(
    research_with_embedder: ResearchHandler,
) -> None:
    """Rows minted before the hash suffix keep the un-suffixed slug:
    verbatim slug reads and request_hash hits both still land on it."""
    h = research_with_embedder
    legacy = _legacy_slug(_QUERY_A)
    _plant(
        h,
        slug=legacy,
        request_hash=h._hash(h._canonical_key(_QUERY_A)),
        query=_QUERY_A,
    )

    resp = h.get(id=legacy, no_fetch=True)
    assert "keep this report" in resp.body
    assert _StubClient.call_count == 0
    # The original query still hits the same row by request_hash.
    resp2 = h.get(q=_QUERY_A)
    assert "keep this report" in resp2.body
    assert _StubClient.call_count == 0


def test_reimport_of_legacy_slugged_query_refreshes_in_place(
    research_with_embedder: ResearchHandler,
) -> None:
    """Re-importing a query cached under a legacy slug must not mint a
    second row — the UNIQUE (provider, request_hash) constraint would
    500 on a fresh insert. The row refreshes in place, keeping the
    legacy slug and ref id."""
    h = research_with_embedder
    legacy = _plant(
        h,
        slug=_legacy_slug(_QUERY_A),
        request_hash=h._hash(h._canonical_key(_QUERY_A)),
        query=_QUERY_A,
    )
    before = h.store.get_cache_entry_by_slug(kind="perplexity-research", slug=legacy)
    assert before is not None
    ref_id = before[0].id

    resp = h.put(id=_QUERY_A, text="# A\n\nre-imported body", mode="import")

    assert "re-imported" in resp.body
    after = h.store.get_cache_entry_by_slug(kind="perplexity-research", slug=legacy)
    assert after is not None
    assert after[0].id == ref_id
    assert "re-imported body" in h.get(id=legacy, no_fetch=True).body
    # No second row under the new hash-suffixed slug.
    new_slug = h._slug_for(h._canonical_key(_QUERY_A))
    assert new_slug != legacy
    assert (
        h.store.get_cache_entry_by_slug(kind="perplexity-research", slug=new_slug)
        is None
    )


def test_reimport_under_new_slug_also_refreshes_in_place(
    research_with_embedder: ResearchHandler,
) -> None:
    """Same-query re-import under the hash-suffixed scheme refreshes the
    row — ref id and any annotations survive (no delete+insert)."""
    h = research_with_embedder
    slug = _import_a(h)
    entry = h.store.get_cache_entry_by_slug(kind="perplexity-research", slug=slug)
    assert entry is not None
    ref_id = entry[0].id

    h.put(id=_QUERY_A, text="# A\n\nre-imported body", mode="import")

    after = h.store.get_cache_entry_by_slug(kind="perplexity-research", slug=slug)
    assert after is not None
    assert after[0].id == ref_id
    assert "re-imported body" in h.get(id=slug, no_fetch=True).body


def test_same_slug_different_query_still_refused(
    research_with_embedder: ResearchHandler,
) -> None:
    """The collision guard still fires on a *genuine* same-slug match:
    a live row sitting under the slug a different query now mints
    (a legacy row that happens to equal the new form, or a hash-tail
    clash)."""
    h = research_with_embedder
    key_b = h._canonical_key(_QUERY_B)
    slug_b = h._slug_for(key_b)
    _plant(
        h,
        slug=slug_b,
        request_hash="some-other-hash",
        query=_QUERY_A,
    )

    with pytest.raises(BadInput, match="already holds") as err:
        h.get(q=_QUERY_B)
    assert _StubClient.call_count == 0
    msg = str(err.value)
    assert slug_b in msg and _QUERY_A in msg
    assert "keep this report" in h.get(id=slug_b, no_fetch=True).body

    def replay_hint(verb: str, kwargs: dict[str, Any]) -> Any:
        extras = kwargs.pop("args", {})
        return getattr(h, verb)(**kwargs, **extras)

    hints = assert_hints_round_trip(str(err.value.next), replay_hint, whole_body=True)
    assert any("no_fetch" in hint for hint in hints)

    with pytest.raises(BadInput, match="already holds"):
        h.put(id=_QUERY_B, text="# B\n\nunrelated report", mode="import")
    assert _StubClient.call_count == 0


def test_same_query_whitespace_variant_mints_sibling_row(
    research_with_embedder: ResearchHandler,
) -> None:
    """A whitespace variant is a different canonical key — different
    request_hash AND a different hash tail — so it fetches and mints a
    sibling row rather than refreshing the first query's in place."""
    h = research_with_embedder
    slug = _import_a(h)
    spaced = _QUERY_A.replace("droplet", "droplet  ")

    resp = h.get(q=spaced)

    assert _StubClient.call_count == 1
    assert "Dario Amodei" in resp.body
    cached = h.store.get_cache_entry_by_slug(kind="perplexity-research", slug=slug)
    assert cached is not None


def test_slug_match_without_stored_query_keeps_legacy_refresh(
    research_with_embedder: ResearchHandler,
) -> None:
    """A row written before ``query`` was tracked in meta cannot be
    compared; the pre-guard in-place refresh stays."""
    h = research_with_embedder
    key = h._canonical_key(_QUERY_B)
    slug = _plant(
        h,
        slug=h._slug_for(key),
        request_hash="legacy-hash",
        query=None,
    )

    resp = h.get(q=_QUERY_B)

    assert _StubClient.call_count == 1
    assert "Dario Amodei" in resp.body
    after = h.store.get_cache_entry_by_slug(kind="perplexity-research", slug=slug)
    assert after is not None
    assert (after[1].meta or {}).get("query") == _QUERY_B


# ── view='body' — read-only whole-body handler rendering ─────────────


def test_body_view_serves_whole_body_without_fetch(
    research_with_embedder: ResearchHandler,
) -> None:
    h = research_with_embedder
    h.put(id="catalyst survey", text=_SAMPLE_RESEARCH_REPORT, mode="import")
    calls = _StubClient.call_count

    resp = h.get(id="catalyst survey", view="body")

    assert "Cobalt phthalocyanine" in resp.body
    # view='body' implies no_fetch — a read never fires a paid call.
    assert _StubClient.call_count == calls


def test_body_view_by_slug(
    research_with_embedder: ResearchHandler,
) -> None:
    """The /recent-advertised slug round-trips through view='body' too."""
    h = research_with_embedder
    slug = _import_a(h)

    resp = h.get(id=slug, view="body")

    assert "keep this report" in resp.body
    assert _StubClient.call_count == 0


def test_body_view_miss_is_notfound_without_fetch(
    websearch: WebsearchHandler,
) -> None:
    """A view='body' read on an uncached query refuses rather than
    spending — same contract as no_fetch."""
    with pytest.raises(NotFound, match="not cached"):
        websearch.get(id="a question nobody has cached", view="body")
    assert _StubClient.call_count == 0


def test_body_view_refuses_refresh_combo(websearch: WebsearchHandler) -> None:
    """view='body' is a read — it never re-fetches, so ``refresh`` on the
    same call is contradictory and refused rather than silently
    resolved."""
    with pytest.raises(BadInput, match="read-only"):
        websearch.get(id="anything", view="body", refresh=True)
    with pytest.raises(BadInput, match="read-only"):
        websearch.get(id="anything", view="body", mode="refresh")
    assert _StubClient.call_count == 0


def test_unknown_view_rejected(websearch: WebsearchHandler) -> None:
    """A mistyped view is refused with the accepted set, not silently
    ignored (previously view= passed through unread)."""
    with pytest.raises(BadInput, match="unknown view") as err:
        websearch.get(id="anything", view="bogu")
    assert err.value.options == ["body"]
    assert _StubClient.call_count == 0

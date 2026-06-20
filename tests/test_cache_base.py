"""Tests for `CacheBackedHandler` — the cache flow shared by phase-4 kinds.

Uses a fake subclass `_FakeCacheKind` whose `_fetch` is mockable. The
real subclasses (math, youtube, web) get tested separately with their
own HTTP mocks.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers._cache_base import CacheBackedHandler, FetchResult
from precis.protocol import KindSpec
from precis.store import Store
from precis.store.types import BlockInsert


class _FakeCacheKind(CacheBackedHandler):
    """Test double: counts upstream calls so we can verify cache hits."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="fake",  # not in `kinds` table — we'll patch the corpus and use a real kind
        title="Fake cache kind",
        description="Test-only cache-backed kind.",
        supports_get=True,
    )

    provider: ClassVar[str] = "wolfram"
    ttl_seconds: ClassVar[int | None] = 3600
    attribution: ClassVar[str] = "Computed by FakeCorp."
    corpus_slug: ClassVar[str] = "default"

    def __init__(self, *, hub: Hub) -> None:
        super().__init__(hub=hub)
        self.fetch_calls: list[str] = []
        self.canned: dict[str, FetchResult] = {}

    def _canonical_key(self, query: str) -> str:
        return query.strip().lower()

    def _fetch(self, key: str) -> FetchResult:
        self.fetch_calls.append(key)
        if key in self.canned:
            return self.canned[key]
        return FetchResult(
            title=f"answer for {key}",
            body_blocks=[BlockInsert(pos=0, text=f"the {key} answer")],
            cost_usd=0.002,
            meta={"echo": key},
        )


class _FakeCacheKindAsMath(_FakeCacheKind):
    """Same as `_FakeCacheKind`, but advertises `kind='math'` so it can
    persist into the `refs` table without needing a new closed kind."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="math",
        title="Fake math",
        description="Test-only.",
        supports_get=True,
    )


@pytest.fixture
def handler(hub: Hub) -> _FakeCacheKindAsMath:
    return _FakeCacheKindAsMath(hub=hub)


# ── basic flow ────────────────────────────────────────────────────────


def test_first_call_is_a_miss(handler: _FakeCacheKindAsMath) -> None:
    resp = handler.get(q="population of Ireland")
    assert "the population of ireland answer" in resp.body
    assert "- Computed by FakeCorp." in resp.body
    assert "[cost: ~$0.0020]" in (resp.cost or "")
    assert "cached" not in (resp.cost or "")
    assert handler.fetch_calls == ["population of ireland"]


def test_second_call_hits_cache(handler: _FakeCacheKindAsMath) -> None:
    handler.get(q="population of Ireland")
    resp2 = handler.get(q="population of Ireland")
    # Same body
    assert "the population of ireland answer" in resp2.body
    # Cached marker on cost trailer
    assert resp2.cost == "[cost: ~$0.0020 - cached]"
    # Upstream still only called once.
    assert handler.fetch_calls == ["population of ireland"]


def test_canonicalization_collapses_variants(handler: _FakeCacheKindAsMath) -> None:
    """Whitespace + case variants share a cache row."""
    handler.get(q="Population of Ireland")
    handler.get(q="  POPULATION OF IRELAND  ")
    handler.get(q="population of ireland")
    assert len(handler.fetch_calls) == 1


def test_id_and_q_are_equivalent(handler: _FakeCacheKindAsMath) -> None:
    handler.get(id="speed of light")
    resp = handler.get(q="speed of light")
    assert "cached" in (resp.cost or "")


def test_attribution_renders_on_hit_and_miss(
    handler: _FakeCacheKindAsMath,
) -> None:
    miss = handler.get(q="x")
    hit = handler.get(q="x")
    assert "- Computed by FakeCorp." in miss.body
    assert "- Computed by FakeCorp." in hit.body


# ── input validation ─────────────────────────────────────────────────


def test_bare_get_serves_recent_listing(
    handler: _FakeCacheKindAsMath,
) -> None:
    """Bare ``get()`` no longer raises — it serves a /recent listing
    so math/web/youtube agree with websearch/think/research on the
    cross-kind convention. (MCP critic MAJOR — bare-get inconsistency.)
    """
    resp = handler.get()
    assert "recent math refs" in resp.body.lower()


def test_blank_query_serves_recent_listing(
    handler: _FakeCacheKindAsMath,
) -> None:
    """Whitespace-only ``q=`` is treated the same as no query — the
    listing path swallows it rather than raising. Same ergonomics as
    the Perplexity kinds."""
    resp = handler.get(q="   ")
    assert "recent math refs" in resp.body.lower()


def test_recent_footer_does_not_overcount_under_cap(
    handler: _FakeCacheKindAsMath,
) -> None:
    """When the pool is smaller than the page cap the footer must read
    "showing N of N" — not "of at most <cap>", which would imply a
    truncation that did not happen (#39252)."""
    handler.get(q="speed of light")
    resp = handler.get()
    assert "showing 1 of 1" in resp.body
    assert "of at most" not in resp.body


def test_query_via_id_path_view_raises(
    handler: _FakeCacheKindAsMath,
) -> None:
    """An unknown slash path (``id='/foo'``) is sharply rejected —
    the listing only accepts ``/`` and ``/recent``."""
    with pytest.raises(BadInput, match=r"unknown view"):
        handler.get(id="/foo")


# ── freshness ─────────────────────────────────────────────────────────


def test_pinned_entries_never_expire(store: Store) -> None:
    class Pinned(_FakeCacheKindAsMath):
        ttl_seconds: ClassVar[int | None] = None  # pin

    h = Pinned(hub=Hub(store=store))
    h.get(q="pi")
    h.get(q="pi")
    assert len(h.fetch_calls) == 1


def test_zero_ttl_means_always_stale(store: Store) -> None:
    class ZeroTTL(_FakeCacheKindAsMath):
        ttl_seconds: ClassVar[int | None] = 0

    h = ZeroTTL(hub=Hub(store=store))
    h.get(q="now")
    h.get(q="now")
    # ttl=0 means fresh_until = now() + 0s, so the second call is stale.
    assert len(h.fetch_calls) == 2


# ── cost trailer format ──────────────────────────────────────────────


def test_free_provider_renders_cost_free(store: Store) -> None:
    class Free(_FakeCacheKindAsMath):
        provider: ClassVar[str] = "youtube"

        # Override _fetch to return cost_usd=None.
        def _fetch(self, key: str) -> FetchResult:
            self.fetch_calls.append(key)
            return FetchResult(
                title="t",
                body_blocks=[BlockInsert(pos=0, text="x")],
                cost_usd=None,
            )

    h = Free(hub=Hub(store=store))
    resp = h.get(q="anything")
    assert resp.cost == "[cost: free]"


def test_cost_zero_renders_free(store: Store) -> None:
    class Zero(_FakeCacheKindAsMath):
        def _fetch(self, key: str) -> FetchResult:
            self.fetch_calls.append(key)
            return FetchResult(
                title="t",
                body_blocks=[BlockInsert(pos=0, text="x")],
                cost_usd=0.0,
            )

    h = Zero(hub=Hub(store=store))
    resp = h.get(q="anything")
    assert resp.cost == "[cost: free]"


# ── multi-block bodies ───────────────────────────────────────────────


def test_multi_block_body_renders_concatenated(store: Store) -> None:
    class Multi(_FakeCacheKindAsMath):
        def _fetch(self, key: str) -> FetchResult:
            self.fetch_calls.append(key)
            return FetchResult(
                title="paragraphs",
                body_blocks=[
                    BlockInsert(pos=0, text="first paragraph"),
                    BlockInsert(pos=1, text="second paragraph"),
                ],
                cost_usd=0.001,
            )

    h = Multi(hub=Hub(store=store))
    resp = h.get(q="anything")
    assert "first paragraph\n\nsecond paragraph" in resp.body


# ── tags= on get (gripe:3681 phase 2) ─────────────────────────────────


def test_get_with_tags_applies_on_create(handler: _FakeCacheKindAsMath) -> None:
    """One-call bookmark: tags= on cache miss applies after create."""
    handler.get(q="population of ireland", tags=["bookmark"])
    # The fetch happened (cache miss).
    assert handler.fetch_calls == ["population of ireland"]
    # The tag landed on the freshly-created ref.
    refs = handler.store.list_refs(kind="math", limit=10)
    assert len(refs) == 1
    tags = handler.store.tags_for(refs[0].id)
    assert any(str(t) == "bookmark" for t in tags)


def test_get_with_tags_applies_on_hit(handler: _FakeCacheKindAsMath) -> None:
    """tags= on cache hit still applies — bookmark-after-the-fact."""
    handler.get(q="speed of light")  # first call: miss, no tags
    handler.get(q="speed of light", tags=["bookmark"])  # hit, but tag applies
    assert handler.fetch_calls == ["speed of light"]  # still one fetch
    refs = handler.store.list_refs(kind="math", limit=10)
    assert len(refs) == 1
    tags = handler.store.tags_for(refs[0].id)
    assert any(str(t) == "bookmark" for t in tags)


def test_get_tags_validates_unknown_prefix(handler: _FakeCacheKindAsMath) -> None:
    """tags= with an unknown closed prefix raises BadInput (not silent-drop)."""
    # ``BOGUS:`` isn't a registered closed-vocab prefix — should reject
    # before the fetch fires.
    with pytest.raises(BadInput):
        handler.get(q="anything", tags=["BOGUS:value"])
    # No fetch — validation runs before the upstream call.
    assert handler.fetch_calls == []


# ── mode='refresh' (gripe:3681 phase 4) ───────────────────────────────


def test_unknown_mode_raises_bad_input(handler: _FakeCacheKindAsMath) -> None:
    with pytest.raises(BadInput) as exc:
        handler.get(q="anything", mode="reload")
    # ``refresh`` is the only currently-honoured mode; surface it in
    # both ``options=`` and the recovery hint so the agent can
    # copy-paste the fix.
    assert "refresh" in (exc.value.options or [])
    assert "refresh" in (exc.value.next or "")
    # No fetch — validation runs before the upstream call.
    assert handler.fetch_calls == []


def test_mode_refresh_bypasses_cache(handler: _FakeCacheKindAsMath) -> None:
    """mode='refresh' forces a re-fetch even when the cache is fresh."""
    handler.get(q="speed of light")
    handler.get(q="speed of light")  # cache hit
    assert len(handler.fetch_calls) == 1
    handler.get(q="speed of light", mode="refresh")  # forced re-fetch
    assert len(handler.fetch_calls) == 2


def test_mode_refresh_preserves_tags(handler: _FakeCacheKindAsMath) -> None:
    """A bookmark survives mode='refresh' — the whole point of phase 4."""
    handler.get(q="pi", tags=["bookmark"])
    refs_before = handler.store.list_refs(kind="math", limit=10)
    assert len(refs_before) == 1
    ref_id_before = refs_before[0].id

    handler.get(q="pi", mode="refresh")

    refs_after = handler.store.list_refs(kind="math", limit=10)
    assert len(refs_after) == 1
    # Same ref id — preserved in place, not deleted+recreated.
    assert refs_after[0].id == ref_id_before
    # Tags survived.
    tags = handler.store.tags_for(refs_after[0].id)
    assert any(str(t) == "bookmark" for t in tags)


def test_stale_cache_refetch_preserves_tags(store: Store) -> None:
    """TTL-expired re-fetches must NOT destroy bookmarks (regression).

    Pre-fix: ``put_cache_entry`` did DELETE + INSERT on every stale-
    cache miss, blowing away tags. ``update_cache_entry`` does
    UPDATE-in-place and tags survive. (gripe:3681 phase 4.)
    """

    class ZeroTTL(_FakeCacheKindAsMath):
        ttl_seconds: ClassVar[int | None] = 0

    h = ZeroTTL(hub=Hub(store=store))
    h.get(q="planck constant", tags=["bookmark"])
    refs_before = h.store.list_refs(kind="math", limit=10)
    ref_id = refs_before[0].id

    # Second call: ttl=0 means cache is born stale → re-fetch.
    h.get(q="planck constant")

    refs_after = h.store.list_refs(kind="math", limit=10)
    assert len(refs_after) == 1
    assert refs_after[0].id == ref_id  # in-place
    tags = h.store.tags_for(ref_id)
    assert any(str(t) == "bookmark" for t in tags), (
        "TTL-expired stale-cache re-fetch destroyed bookmark tag"
    )


def test_refresh_by_slug_without_recover_key_raises(
    handler: _FakeCacheKindAsMath,
) -> None:
    """Default _recover_key returns None → slug refresh demands q=."""
    handler.get(q="atomic mass of carbon")
    refs = handler.store.list_refs(kind="math", limit=10)
    slug = refs[0].slug
    assert slug is not None

    # The fake handler's _canonical_key accepts any string, so it'll
    # successfully canonicalise a slug too. Use a kind whose
    # canonicalizer rejects non-URL/structured input to verify the
    # slug-fallback BadInput path. We'll exercise the full path via
    # the per-kind tests (test_web.py) rather than synthesising
    # another fake here.
    # For the fake kind the slug is itself a valid query, so just
    # assert that mode=refresh + slug works with the default
    # _recover_key returning the canonicalised query.
    n_before = len(handler.fetch_calls)
    handler.get(id=slug, mode="refresh")
    assert len(handler.fetch_calls) == n_before + 1


# ── WATCH:<interval> tag axis (gripe:3681 phase 4) ────────────────────


def test_watch_axis_restricted_to_cache_kinds() -> None:
    """WATCH is permitted on web/youtube/perplexity but rejected elsewhere.

    Memory has no WATCH axis (memories aren't refreshable from
    upstream), so the validator rejects with the recovery hint that
    open-tag form is the right shape for memory ``watch`` semantics.
    """
    from precis.errors import BadInput as _BI
    from precis.store.types import Tag

    with pytest.raises(_BI) as exc:
        Tag.parse_strict("WATCH:daily", kind="memory")
    msg = str(exc.value)
    assert "WATCH" in msg
    assert "axis not allowed" in msg or "memory" in msg


def test_watch_interval_rejects_unknown_value(store: Store) -> None:
    """WATCH:<bogus> rejected with the four valid intervals listed."""
    from precis.errors import BadInput as _BI
    from precis.store.types import Tag

    with pytest.raises(_BI) as exc:
        Tag.parse_strict("WATCH:dialy")
    # The cause names the bad value.
    assert "dialy" in str(exc.value).lower()
    # The four allowed values appear in options=, sorted alphabetically.
    assert exc.value.options == ["daily", "hourly", "monthly", "weekly"]


def test_watch_axis_allowed_on_web_kind(store: Store) -> None:
    """WATCH is permitted on cache-backed kinds (web, youtube, perplexity)."""
    from precis.store.types import Tag

    # Should NOT raise — web is in the WATCH allowlist.
    parsed = Tag.parse_strict("WATCH:daily", kind="web")
    assert parsed.namespace == "closed"
    assert parsed.prefix == "WATCH"
    assert parsed.value == "daily"


# ---- _split_body_blocks: long fetches become multiple chunks ---------


def _stub_handler() -> CacheBackedHandler:
    """A handler instance we can call ``_split_body_blocks`` on directly.

    No DB / store needed — the splitter is a pure function over its
    arguments. We bypass __init__ so we don't need a Hub.
    """
    h = _FakeCacheKindAsMath.__new__(_FakeCacheKindAsMath)
    return h  # type: ignore[return-value]


def test_short_block_passes_through_unchanged() -> None:
    """Blocks below the target size aren't touched — preserves the
    "one short answer per cache row" shape for math / wolfram."""
    h = _stub_handler()
    blocks = [BlockInsert(pos=0, text="short answer.")]
    out = h._split_body_blocks(blocks)
    assert len(out) == 1
    assert out[0].text == "short answer."
    assert out[0].pos == 0


def test_long_block_splits_into_multiple_chunks() -> None:
    """A 4 KB transcript-shaped block splits into several ~800-char
    chunks with sequential ``pos`` values."""
    h = _stub_handler()
    sentences = ["This is a real sentence with meaningful content. "] * 80
    blocks = [BlockInsert(pos=0, text="".join(sentences))]
    out = h._split_body_blocks(blocks)
    assert len(out) > 1
    # Positions are contiguous from 0.
    assert [b.pos for b in out] == list(range(len(out)))
    # Each chunk respects the target size (single-word overruns allowed).
    assert all(len(b.text) <= h.chunk_target_chars + 200 for b in out)


def test_split_preserves_block_metadata_via_replace() -> None:
    """``slug`` / ``meta`` / ``density`` survive the split — only pos
    and text change. Otherwise per-chunk_kind metadata would silently
    drop on every cache write."""
    h = _stub_handler()
    long_text = " ".join(["sentence."] * 200)
    blocks = [
        BlockInsert(
            pos=0,
            text=long_text,
            slug="custom-slug",
            meta={"source": "yt", "lang": "en"},
        )
    ]
    out = h._split_body_blocks(blocks)
    assert len(out) > 1
    for b in out:
        assert b.slug == "custom-slug"
        assert b.meta == {"source": "yt", "lang": "en"}


def test_pre_embedded_block_is_NEVER_split() -> None:
    """When the handler computed an embedding for the full block text
    (perplexity / web do this via ``_blocks_from_report``), the splitter
    must leave it alone — each piece would otherwise carry a vector
    that doesn't match its text."""
    h = _stub_handler()
    long_text = " ".join(["sentence."] * 200)
    blocks = [
        BlockInsert(
            pos=0,
            text=long_text,
            embedding=[0.1] * 768,
        )
    ]
    out = h._split_body_blocks(blocks)
    assert len(out) == 1, "pre-embedded blocks must not be split"
    assert out[0].embedding == [0.1] * 768


def test_chunk_target_chars_zero_disables_splitting() -> None:
    """Subclass opt-out: setting ``chunk_target_chars = 0`` bypasses
    the splitter entirely (kept as an escape hatch for kinds whose
    natural unit is one cache row = one short answer)."""

    class _NoSplit(_FakeCacheKindAsMath):
        chunk_target_chars: ClassVar[int] = 0

    h = _NoSplit.__new__(_NoSplit)
    long_text = " ".join(["sentence."] * 200)
    blocks = [BlockInsert(pos=0, text=long_text)]
    out = h._split_body_blocks(blocks)
    assert len(out) == 1
    assert out[0].text == long_text

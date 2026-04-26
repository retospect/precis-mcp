"""Tests for `CacheBackedHandler` — the cache flow shared by phase-4 kinds.

Uses a fake subclass `_FakeCacheKind` whose `_fetch` is mockable. The
real subclasses (math, youtube, web) get tested separately with their
own HTTP mocks.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

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

    def __init__(self, *, store: Store) -> None:
        super().__init__(store=store)
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
def handler(store: Store) -> _FakeCacheKindAsMath:
    return _FakeCacheKindAsMath(store=store)


# ── basic flow ────────────────────────────────────────────────────────


def test_first_call_is_a_miss(handler: _FakeCacheKindAsMath) -> None:
    resp = handler.get(q="population of Ireland")
    assert "the population of ireland answer" in resp.body
    assert "— Computed by FakeCorp." in resp.body
    assert "[cost: ~$0.0020]" in (resp.cost or "")
    assert "cached" not in (resp.cost or "")
    assert handler.fetch_calls == ["population of ireland"]


def test_second_call_hits_cache(handler: _FakeCacheKindAsMath) -> None:
    handler.get(q="population of Ireland")
    resp2 = handler.get(q="population of Ireland")
    # Same body
    assert "the population of ireland answer" in resp2.body
    # Cached marker on cost trailer
    assert resp2.cost == "[cost: ~$0.0020 — cached]"
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
    assert "— Computed by FakeCorp." in miss.body
    assert "— Computed by FakeCorp." in hit.body


# ── input validation ─────────────────────────────────────────────────


def test_missing_query_raises(handler: _FakeCacheKindAsMath) -> None:
    with pytest.raises(BadInput, match="require a query"):
        handler.get()


def test_blank_query_raises(handler: _FakeCacheKindAsMath) -> None:
    with pytest.raises(BadInput):
        handler.get(q="   ")


# ── freshness ─────────────────────────────────────────────────────────


def test_pinned_entries_never_expire(store: Store) -> None:
    class Pinned(_FakeCacheKindAsMath):
        ttl_seconds: ClassVar[int | None] = None  # pin

    h = Pinned(store=store)
    h.get(q="pi")
    h.get(q="pi")
    assert len(h.fetch_calls) == 1


def test_zero_ttl_means_always_stale(store: Store) -> None:
    class ZeroTTL(_FakeCacheKindAsMath):
        ttl_seconds: ClassVar[int | None] = 0

    h = ZeroTTL(store=store)
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

    h = Free(store=store)
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

    h = Zero(store=store)
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

    h = Multi(store=store)
    resp = h.get(q="anything")
    assert "first paragraph\n\nsecond paragraph" in resp.body

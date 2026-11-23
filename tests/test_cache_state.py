"""Tests for `Store.get_cache_entry` / `put_cache_entry` — the cache_state CRUD that phase 4 cache-backed kinds will use."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from precis.store import ChunkInsert, Store
from precis.store.types import CacheEntry, Ref, Tag


def test_put_and_get_cache_entry_roundtrip(store: Store) -> None:
    ref, cache = store.put_cache_entry(
        kind="math",
        slug="population-of-ireland",
        title="population of Ireland",
        body_blocks=[
            ChunkInsert(ord=0, text="Approximately 5.1 million as of 2024."),
        ],
        provider="wolfram",
        request_hash="abc123",
        ttl_seconds=3600,
        cost_usd=0.002,
        cache_meta={"input_query": "population of Ireland"},
    )
    assert isinstance(ref, Ref)
    assert isinstance(cache, CacheEntry)
    assert ref.kind == "math"
    assert ref.slug == "population-of-ireland"
    assert cache.provider == "wolfram"
    assert cache.request_hash == "abc123"
    assert cache.cost_usd == pytest.approx(0.002)
    assert cache.fresh_until is not None
    assert cache.meta == {"input_query": "population of Ireland"}

    # Lookup by (provider, request_hash) round-trips.
    found = store.get_cache_entry(provider="wolfram", request_hash="abc123")
    assert found is not None
    f_ref, f_cache = found
    assert f_ref.id == ref.id
    assert f_cache.ref_id == cache.ref_id
    assert f_cache.cost_usd == pytest.approx(0.002)


def test_get_cache_entry_miss(store: Store) -> None:
    assert (
        store.get_cache_entry(provider="wolfram", request_hash="never-cached") is None
    )


def test_pinned_entry_has_null_fresh_until(store: Store) -> None:
    ref, cache = store.put_cache_entry(
        kind="math",
        slug="pi",
        title="π",
        body_blocks=[ChunkInsert(ord=0, text="3.14159...")],
        provider="wolfram",
        request_hash="pi-hash",
        ttl_seconds=None,
    )
    assert cache.fresh_until is None  # pinned


def test_replace_existing_cache_entry(store: Store) -> None:
    """Re-fetching a query (same provider + hash) replaces the body."""
    store.put_cache_entry(
        kind="math",
        slug="speed-of-light",
        title="speed of light",
        body_blocks=[ChunkInsert(ord=0, text="299792458 m/s")],
        provider="wolfram",
        request_hash="sol-hash",
        ttl_seconds=3600,
    )
    # Re-cache with different body — same kind+slug, so the existing
    # ref is hard-deleted and re-inserted, cascading away the old
    # blocks and old cache_state row.
    ref2, cache2 = store.put_cache_entry(
        kind="math",
        slug="speed-of-light",
        title="speed of light (revised)",
        body_blocks=[
            ChunkInsert(ord=0, text="299_792_458 m/s — exact"),
            ChunkInsert(ord=1, text="≈ 3 × 10⁸ m/s"),
        ],
        provider="wolfram",
        request_hash="sol-hash",
        ttl_seconds=7200,
    )
    assert ref2.title == "speed of light (revised)"
    assert store.chunks.count_chunks(ref2.id) == 2

    # Lookup returns the new entry.
    found = store.get_cache_entry(provider="wolfram", request_hash="sol-hash")
    assert found is not None
    _, cache_found = found
    assert cache_found.ref_id == ref2.id


def test_soft_deleted_ref_is_not_a_cache_hit(store: Store) -> None:
    ref, _ = store.put_cache_entry(
        kind="math",
        slug="soft-del",
        title="soft delete test",
        body_blocks=[ChunkInsert(ord=0, text="x")],
        provider="wolfram",
        request_hash="soft-hash",
        ttl_seconds=3600,
    )
    store.retire_ref(ref.id)
    assert store.get_cache_entry(provider="wolfram", request_hash="soft-hash") is None


def test_freshness_logic_via_view(store: Store) -> None:
    """`fresh_until > now()` ⇒ caller treats it as fresh.

    The Store doesn't make the freshness decision (that's the
    handler's job using the application clock), but we verify the
    `fresh_until` column is correctly set in the past for ttl=0 and
    the future for ttl>0.
    """
    _, fresh = store.put_cache_entry(
        kind="math",
        slug="fresh",
        title="fresh",
        body_blocks=[ChunkInsert(ord=0, text="x")],
        provider="wolfram",
        request_hash="fresh",
        ttl_seconds=3600,
    )
    _, stale = store.put_cache_entry(
        kind="math",
        slug="stale",
        title="stale",
        body_blocks=[ChunkInsert(ord=0, text="x")],
        provider="wolfram",
        request_hash="stale",
        ttl_seconds=0,
    )

    now = datetime.now(UTC)
    assert fresh.fresh_until is not None and fresh.fresh_until > now
    assert stale.fresh_until is not None and stale.fresh_until <= now + timedelta(
        seconds=1
    )


def test_two_providers_share_request_hash(store: Store) -> None:
    """Same `request_hash` under different providers → distinct rows."""
    store.put_cache_entry(
        kind="math",
        slug="m1",
        title="m1",
        body_blocks=[ChunkInsert(ord=0, text="m")],
        provider="wolfram",
        request_hash="shared",
        ttl_seconds=3600,
    )
    store.put_cache_entry(
        kind="web",
        slug="w1",
        title="w1",
        body_blocks=[ChunkInsert(ord=0, text="w")],
        provider="web",
        request_hash="shared",
        ttl_seconds=3600,
    )
    a = store.get_cache_entry(provider="wolfram", request_hash="shared")
    b = store.get_cache_entry(provider="web", request_hash="shared")
    assert a is not None and b is not None
    assert a[0].kind == "math"
    assert b[0].kind == "web"


def test_blocks_persist_with_cache_entry(store: Store) -> None:
    """`body_blocks` insert is part of the same transaction as the cache row."""
    ref, _ = store.put_cache_entry(
        kind="math",
        slug="multi",
        title="multi-block",
        body_blocks=[
            ChunkInsert(ord=0, text="line 1"),
            ChunkInsert(ord=1, text="line 2"),
            ChunkInsert(ord=2, text="line 3"),
        ],
        provider="wolfram",
        request_hash="multi",
        ttl_seconds=3600,
    )
    blocks = store.chunks.list_chunks_for_ref(ref.id)
    assert [b.ord for b in blocks] == [0, 1, 2]
    assert [b.text for b in blocks] == ["line 1", "line 2", "line 3"]


def test_cost_usd_optional(store: Store) -> None:
    """Free providers (e.g. youtube) leave cost_usd null."""
    _, cache = store.put_cache_entry(
        kind="youtube",
        slug="abc",
        title="t",
        body_blocks=[ChunkInsert(ord=0, text="transcript")],
        provider="youtube",
        request_hash="vid:abc",
        ttl_seconds=86400 * 30,
        cost_usd=None,
    )
    assert cache.cost_usd is None


@pytest.mark.parametrize(
    ("kind", "provider"),
    [
        ("web", "web"),
        ("youtube", "youtube"),
        ("wikipedia", "wikipedia"),
        ("websearch", "perplexity"),
        ("perplexity-research", "perplexity"),
        ("perplexity-reasoning", "perplexity"),
    ],
)
def test_refresh_preserves_chunk_links_and_advances_query(
    store: Store, kind: str, provider: str
) -> None:
    slug, key = f"{kind}-reported", "request-key"
    original, _ = store.put_cache_entry(
        kind=kind,
        slug=slug,
        provider=provider,
        request_hash=key,
        title="First result",
        body_blocks=[ChunkInsert(ord=0, text="Cited text")],
        ttl_seconds=0,
    )
    bookmark = Tag.open("bookmark")
    store.add_tag(original.id, bookmark)
    note = store.insert_ref(kind="memory", slug=None, title="Citation")
    link = store.add_link(
        src_ref_id=note.id, dst_ref_id=original.id, dst_pos=0, relation="cites"
    )

    second, _ = store.update_cache_entry(
        ref_id=original.id,
        title="Second result",
        body_blocks=[ChunkInsert(ord=0, text="Updated text")],
        ttl_seconds=None,
    )
    third, _ = store.update_cache_entry(
        ref_id=original.id,
        title="Third result",
        body_blocks=[ChunkInsert(ord=0, text="Latest text")],
        ttl_seconds=None,
    )

    assert len({original.id, second.id, third.id}) == 3
    cited = store.get_cache_entry_by_slug(kind=kind, slug=slug)
    assert cited is not None and cited[0].id == original.id
    old_chunk = store.chunks.list_chunks_for_ref(original.id)[0]
    assert old_chunk.text == "Cited text"
    (saved,) = store.links_for(note.id)
    assert (saved.id, saved.dst_chunk_id) == (link.id, old_chunk.id)
    latest = store.get_cache_entry(provider=provider, request_hash=key)
    assert latest is not None and latest[0].id == third.id
    assert [row[0].id for row in store.list_cache_entries(kind=kind)] == [third.id]
    assert store.count_cache_entries(kind=kind) == 1
    assert bookmark in store.tags_for(original.id)
    assert bookmark in store.tags_for(third.id)
    assert any(
        edge.dst_ref_id == second.id and edge.relation == "supersedes"
        for edge in store.links_for(third.id)
    )

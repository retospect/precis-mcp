"""Tests for `Store.get_cache_entry` / `put_cache_entry` — the cache_state CRUD that phase 4 cache-backed kinds will use."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from precis.store import BlockInsert, Store
from precis.store.types import CacheEntry, Ref


def test_put_and_get_cache_entry_roundtrip(store: Store) -> None:
    ref, cache = store.put_cache_entry(
        kind="math",
        slug="population-of-ireland",
        title="population of Ireland",
        body_blocks=[
            BlockInsert(pos=0, text="Approximately 5.1 million as of 2024."),
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
        body_blocks=[BlockInsert(pos=0, text="3.14159...")],
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
        body_blocks=[BlockInsert(pos=0, text="299792458 m/s")],
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
            BlockInsert(pos=0, text="299_792_458 m/s — exact"),
            BlockInsert(pos=1, text="≈ 3 × 10⁸ m/s"),
        ],
        provider="wolfram",
        request_hash="sol-hash",
        ttl_seconds=7200,
    )
    assert ref2.title == "speed of light (revised)"
    assert store.count_blocks(ref2.id) == 2

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
        body_blocks=[BlockInsert(pos=0, text="x")],
        provider="wolfram",
        request_hash="soft-hash",
        ttl_seconds=3600,
    )
    store.soft_delete_ref(ref.id)
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
        body_blocks=[BlockInsert(pos=0, text="x")],
        provider="wolfram",
        request_hash="fresh",
        ttl_seconds=3600,
    )
    _, stale = store.put_cache_entry(
        kind="math",
        slug="stale",
        title="stale",
        body_blocks=[BlockInsert(pos=0, text="x")],
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
        body_blocks=[BlockInsert(pos=0, text="m")],
        provider="wolfram",
        request_hash="shared",
        ttl_seconds=3600,
    )
    store.put_cache_entry(
        kind="web",
        slug="w1",
        title="w1",
        body_blocks=[BlockInsert(pos=0, text="w")],
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
            BlockInsert(pos=0, text="line 1"),
            BlockInsert(pos=1, text="line 2"),
            BlockInsert(pos=2, text="line 3"),
        ],
        provider="wolfram",
        request_hash="multi",
        ttl_seconds=3600,
    )
    blocks = store.list_blocks_for_ref(ref.id)
    assert [b.pos for b in blocks] == [0, 1, 2]
    assert [b.text for b in blocks] == ["line 1", "line 2", "line 3"]


def test_cost_usd_optional(store: Store) -> None:
    """Free providers (e.g. youtube) leave cost_usd null."""
    _, cache = store.put_cache_entry(
        kind="youtube",
        slug="abc",
        title="t",
        body_blocks=[BlockInsert(pos=0, text="transcript")],
        provider="youtube",
        request_hash="vid:abc",
        ttl_seconds=86400 * 30,
        cost_usd=None,
    )
    assert cache.cost_usd is None

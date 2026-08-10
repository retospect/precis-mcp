"""``Store.upsert_stub_paper``'s ``s2_meta`` param — the mint-time S2
metadata pass-through (docs/backlog: skip ``stub_rank``'s enrich round-trip
for a stub that already carries S2 data at mint).

Covers the store-engine contract: a fresh mint merges ``s2_meta`` into the
initial ``refs.meta`` (``set_by`` wins any collision, though there is
none in practice); a collapse hit onto an existing ref never touches
``meta`` — mint-time only, no retroactive patching (see
``upsert_stub_paper``'s own docstring).
"""

from __future__ import annotations

from datetime import UTC, datetime

from precis.ingest.semantic_scholar import s2_stub_meta
from precis.store import Store


def _ref_meta(store: Store, ref_id: int) -> dict[str, object]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


def test_mint_merges_s2_meta_into_initial_meta(store: Store) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    patch = s2_stub_meta(
        {"abstract": "an abstract", "fields": ["Physics"], "citation_count": 3},
        now=now,
    )
    ref_id, created = store.upsert_stub_paper(
        identifiers=[("doi", "10.1/mint-s2-meta")],
        title="A Fresh Stub",
        year=2025,
        set_by="system",
        s2_meta=patch,
    )
    assert created is True
    meta = _ref_meta(store, ref_id)
    assert meta["s2_enriched_at"] == now.isoformat()
    assert meta["s2_fields"] == ["Physics"]
    assert meta["s2_citation_count"] == 3
    assert meta["abstract"] == "an abstract"
    assert meta["set_by"] == "system"


def test_mint_with_no_s2_meta_writes_only_set_by(store: Store) -> None:
    ref_id, created = store.upsert_stub_paper(
        identifiers=[("doi", "10.1/mint-no-s2-meta")],
        title="Another Fresh Stub",
        set_by="dream",
    )
    assert created is True
    meta = _ref_meta(store, ref_id)
    assert meta == {"set_by": "dream"}


def test_existing_ref_hit_does_not_write_s2_meta(store: Store) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    ref_id, created = store.upsert_stub_paper(
        identifiers=[("doi", "10.1/existing-hit")],
        title="Already Wanted",
        set_by="dream",
    )
    assert created is True
    before = _ref_meta(store, ref_id)
    assert "s2_enriched_at" not in before

    patch = s2_stub_meta({"abstract": "should not land"}, now=now)
    hit_ref_id, hit_created = store.upsert_stub_paper(
        identifiers=[("doi", "10.1/existing-hit")],
        title="Already Wanted",
        set_by="system",
        s2_meta=patch,
    )
    assert hit_created is False
    assert hit_ref_id == ref_id
    after = _ref_meta(store, ref_id)
    assert after == before
    assert "s2_enriched_at" not in after

"""Regression for gr341498: merge_duplicate must repoint bib entries.

Before the fix, ``merge_duplicate`` migrated external identifiers and graph
links to the survivor but left ``paper_bib_entries.held_ref_id`` pointing at
the now-retired duplicate forever — nothing else re-checks a bib entry once
``match_conf`` is set, so the citation stayed "held" against a soft-deleted
ref.
"""

from __future__ import annotations

from typing import Any

from precis.ingest.dedup import merge_duplicate
from precis.store import Store


def _mk_paper(store: Store, *, slug: str) -> int:
    return int(store.insert_ref(kind="paper", slug=slug, title=f"T {slug}").id)


def _seed_bib_entry(store: Store, ref_id: int, marker: int, *, held_ref_id: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO paper_bib_entries "
            "(ref_id, marker, raw_text, held_ref_id, parse_version) "
            "VALUES (%s, %s, %s, %s, 1) RETURNING id",
            (ref_id, marker, f"raw {marker}", held_ref_id),
        ).fetchone()
        conn.commit()
    assert row is not None
    return int(row[0])


def _held_ref_id(store: Store, bib_entry_id: int) -> Any:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT held_ref_id FROM paper_bib_entries WHERE id = %s",
            (bib_entry_id,),
        ).fetchone()
    assert row is not None
    return row[0]


def test_merge_duplicate_repoints_bib_entries_to_survivor(store: Store) -> None:
    survivor = _mk_paper(store, slug="repoint-survivor")
    duplicate = _mk_paper(store, slug="repoint-duplicate")
    citing = _mk_paper(store, slug="repoint-citing")
    entry = _seed_bib_entry(store, citing, 7, held_ref_id=duplicate)

    with store.tx() as conn:
        merge_duplicate(
            store,
            survivor_ref_id=survivor,
            duplicate_ref_id=duplicate,
            source="test",
            reason="unit-test",
            conn=conn,
        )

    assert _held_ref_id(store, entry) == survivor


def test_merge_duplicate_leaves_unrelated_bib_entries_alone(store: Store) -> None:
    survivor = _mk_paper(store, slug="repoint-survivor2")
    duplicate = _mk_paper(store, slug="repoint-duplicate2")
    other_held = _mk_paper(store, slug="repoint-other")
    citing = _mk_paper(store, slug="repoint-citing2")
    entry = _seed_bib_entry(store, citing, 8, held_ref_id=other_held)

    with store.tx() as conn:
        merge_duplicate(
            store,
            survivor_ref_id=survivor,
            duplicate_ref_id=duplicate,
            source="test",
            reason="unit-test",
            conn=conn,
        )

    assert _held_ref_id(store, entry) == other_held

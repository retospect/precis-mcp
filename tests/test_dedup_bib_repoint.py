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


def _doi_of(store: Store, ref_id: int) -> Any:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT id_value FROM ref_identifiers "
            "WHERE ref_id = %s AND id_kind = 'doi'",
            (ref_id,),
        ).fetchone()
    return str(row[0]) if row else None


def test_merge_drops_duplicate_ids_the_survivor_cleared(store: Store) -> None:
    """gr515246: a human cleared the survivor's DOI — a duplicate row
    carrying that cleared value must not resurrect it onto the
    survivor; a different doi on the duplicate still migrates."""
    survivor = _mk_paper(store, slug="merge-clr-surv")
    duplicate = _mk_paper(store, slug="merge-clr-dup")
    with store.tx() as conn:
        store.set_ref_identifier(
            survivor, "doi", "10.9/wrong", source="system", conn=conn
        )
        store.set_ref_identifier(
            duplicate, "arxiv", "2401.00001", source="system", conn=conn
        )
    assert store.clear_ref_identifier(survivor, "doi", source="web-edit")
    # The duplicate carries the cleared value plus one of its own: only
    # the cleared one is dropped by the merge.
    store.insert_ref_identifiers(
        duplicate,
        [("doi", "10.9/wrong", "test"), ("doi", "10.9/dup", "test")],
    )

    with store.tx() as conn:
        merge_duplicate(
            store,
            survivor_ref_id=survivor,
            duplicate_ref_id=duplicate,
            source="test",
            reason="unit-test",
            conn=conn,
        )

    # The cleared value stayed off the survivor; the duplicate's own
    # doi and other kinds migrated.
    assert _dois_of(store, survivor) == ["10.9/dup"]
    assert store.identifiers_for_refs([survivor])[survivor]["arxiv"] == "2401.00001"


def test_merge_unions_cleared_marker_onto_survivor(store: Store) -> None:
    """gr515246: the LOSER held the cleared marker — the survivor must
    inherit it and drop its own identifier row for the cleared scheme,
    so a human clear survives whichever ref wins the merge."""
    cleared_loser = _mk_paper(store, slug="merge-loser-clr")
    survivor = _mk_paper(store, slug="merge-winner")
    with store.tx() as conn:
        store.set_ref_identifier(
            cleared_loser, "doi", "10.9/shared", source="system", conn=conn
        )
    assert store.clear_ref_identifier(cleared_loser, "doi", source="web-edit")
    # The freed value is now owned by the eventual survivor.
    with store.tx() as conn:
        store.set_ref_identifier(
            survivor, "doi", "10.9/shared", source="system", conn=conn
        )
        store.stamp_ref_meta(survivor, {"doi": "10.9/shared"}, conn=conn)

    with store.tx() as conn:
        merge_duplicate(
            store,
            survivor_ref_id=survivor,
            duplicate_ref_id=cleared_loser,
            source="test",
            reason="unit-test",
            conn=conn,
        )

    assert _doi_of(store, survivor) is None
    meta = store.fetch_refs_by_ids([survivor])[survivor].meta
    assert meta["cleared_identifiers"]["doi"]["by"] == "web-edit"
    # …and the display ghost is gone too.
    assert "doi" not in meta


def _dois_of(store: Store, ref_id: int) -> list[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT id_value FROM ref_identifiers "
            "WHERE ref_id = %s AND id_kind = 'doi' ORDER BY id_value",
            (ref_id,),
        ).fetchall()
    return [str(r[0]) for r in rows]


def test_merge_keeps_survivor_own_doi_under_per_value_rule(store: Store) -> None:
    """Per-value marks (gr515246): the loser cleared doi X, the survivor
    holds its own doi Y — Y is not the cleared identifier, so it stays
    and the scheme is not unioned. A marked scheme the survivor does not
    hold (s2) still unions across."""
    cleared_loser = _mk_paper(store, slug="pv-loser")
    survivor = _mk_paper(store, slug="pv-surv")
    with store.tx() as conn:
        store.insert_ref_identifiers(
            cleared_loser,
            [("doi", "10.9/wrong", "test"), ("s2", "c" * 40, "test")],
        )
        store.set_ref_identifier(
            survivor, "doi", "10.9/right", source="system", conn=conn
        )
    assert store.clear_ref_identifier(cleared_loser, "doi", source="web-edit")
    assert store.clear_ref_identifier(cleared_loser, "s2", source="web-edit")

    with store.tx() as conn:
        merge_duplicate(
            store,
            survivor_ref_id=survivor,
            duplicate_ref_id=cleared_loser,
            source="test",
            reason="unit-test",
            conn=conn,
        )

    assert _dois_of(store, survivor) == ["10.9/right"]
    meta = store.fetch_refs_by_ids([survivor])[survivor].meta
    marked = meta.get("cleared_identifiers") or {}
    # doi Y differs from cleared X → the scheme does not union; s2 the
    # survivor never held → carried so re-derivation stays blocked.
    assert "doi" not in marked
    assert marked["s2"]["by"] == "web-edit"


def test_merge_drops_loser_mixed_case_cleared_doi(store: Store) -> None:
    """gr515246: marker ``values`` are recorded post-normalisation
    (lowercase for doi) but a pre-0049 legacy row can still carry the
    publisher's mixed case — the per-value drop must compare
    case-insensitively or the cleared DOI rides across on the loser."""
    survivor = _mk_paper(store, slug="mc-surv")
    duplicate = _mk_paper(store, slug="mc-dup")
    with store.tx() as conn:
        store.set_ref_identifier(
            survivor, "doi", "10.9/wrong", source="system", conn=conn
        )
    assert store.clear_ref_identifier(survivor, "doi", source="web-edit")
    with store.tx() as conn:
        # Mint a legacy mixed-case row: the lowercase trigger + check
        # (0049) stop new writes, so minting one means suspending both —
        # all inside this one tx so a failure rolls the DDL back too.
        conn.execute(
            "ALTER TABLE ref_identifiers "
            "DISABLE TRIGGER trg_ref_identifiers_lowercase_doi"
        )
        conn.execute(
            "ALTER TABLE ref_identifiers DROP CONSTRAINT ref_identifiers_doi_lc"
        )
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES ('doi', '10.9/WRONG', %s, 'test')",
            (duplicate,),
        )
        conn.execute(
            "ALTER TABLE ref_identifiers "
            "ENABLE TRIGGER trg_ref_identifiers_lowercase_doi"
        )
        conn.execute(
            "ALTER TABLE ref_identifiers "
            "ADD CONSTRAINT ref_identifiers_doi_lc "
            "CHECK (id_kind <> 'doi' OR id_value = lower(id_value)) NOT VALID"
        )

    with store.tx() as conn:
        merge_duplicate(
            store,
            survivor_ref_id=survivor,
            duplicate_ref_id=duplicate,
            source="test",
            reason="unit-test",
            conn=conn,
        )

    assert _dois_of(store, survivor) == []
    assert _dois_of(store, duplicate) == []


def test_merge_unions_cleared_values_from_both_refs(store: Store) -> None:
    """gr515246: the survivor cleared doi X1 and the loser cleared doi
    X2 — the unioned marker must keep BOTH values; overwriting the
    survivor's entry would let a later sweep re-derive X1."""
    survivor = _mk_paper(store, slug="uv-surv")
    cleared_loser = _mk_paper(store, slug="uv-loser")
    with store.tx() as conn:
        store.set_ref_identifier(survivor, "doi", "10.9/x1", source="system", conn=conn)
        store.set_ref_identifier(
            cleared_loser, "doi", "10.9/x2", source="system", conn=conn
        )
    assert store.clear_ref_identifier(survivor, "doi", source="web-edit")
    assert store.clear_ref_identifier(cleared_loser, "doi", source="web-edit")

    with store.tx() as conn:
        merge_duplicate(
            store,
            survivor_ref_id=survivor,
            duplicate_ref_id=cleared_loser,
            source="test",
            reason="unit-test",
            conn=conn,
        )

    meta = store.fetch_refs_by_ids([survivor])[survivor].meta
    marked = meta["cleared_identifiers"]["doi"]
    assert sorted(marked["values"]) == ["10.9/x1", "10.9/x2"]


def test_merge_ignores_cleared_non_migratable_scheme(store: Store) -> None:
    """A cleared cite_key mark never merges: cite_key is content-derived,
    stays on the loser, and must not delete the survivor's own key."""
    cleared_loser = _mk_paper(store, slug="ck-loser")
    survivor = _mk_paper(store, slug="ck-surv")
    with store.tx() as conn:
        store.set_ref_identifier(
            cleared_loser, "cite_key", "loserkey20", source="test", conn=conn
        )
        store.set_ref_identifier(
            survivor, "cite_key", "survkey20", source="test", conn=conn
        )
    assert store.clear_ref_identifier(cleared_loser, "cite_key", source="web-edit")

    with store.tx() as conn:
        merge_duplicate(
            store,
            survivor_ref_id=survivor,
            duplicate_ref_id=cleared_loser,
            source="test",
            reason="unit-test",
            conn=conn,
        )

    ids = store.identifiers_for_refs([survivor])[survivor]
    assert ids["cite_key"] == "survkey20"
    meta = store.fetch_refs_by_ids([survivor])[survivor].meta
    assert "cite_key" not in (meta.get("cleared_identifiers") or {})


def test_stub_fold_drops_survivor_cleared_doi(store: Store) -> None:
    """gr515246: the orphan-stub fold in add.py migrates the stub's
    external ids wholesale — a value the survivor's operator cleared
    must be deleted from the stub's rows, not moved; a different value
    of the same scheme still migrates."""
    from precis.ingest.add import _reconcile_orphan_stub

    survivor = _mk_paper(store, slug="fold-surv")
    stub = _mk_paper(store, slug="fold-stub")
    stem = "cagepaper20"
    with store.tx() as conn:
        # Identical titles keep the token-corroboration + identity gates open.
        conn.execute(
            "UPDATE refs SET title = %s WHERE ref_id = ANY(%s)",
            ("The stability of fullerene cages", [survivor, stub]),
        )
        store.set_ref_identifier(stub, "cite_key", stem, source="test", conn=conn)
        store.set_ref_identifier(stub, "s2", "b" * 40, source="system", conn=conn)
        store.set_ref_identifier(
            survivor, "doi", "10.9/wrong", source="system", conn=conn
        )
    assert store.clear_ref_identifier(survivor, "doi", source="web-edit")
    # The stub carries the cleared value plus one of its own: only the
    # cleared one is dropped by the fold.
    store.insert_ref_identifiers(
        stub,
        [("doi", "10.9/wrong", "test"), ("doi", "10.9/other", "test")],
    )

    with store.tx() as conn:
        folded = _reconcile_orphan_stub(
            store, survivor_ref_id=survivor, file_stem=stem, conn=conn
        )
    assert folded == stub

    ids = store.identifiers_for_refs([survivor])[survivor]
    # The cleared value stayed off the survivor; the stub's own doi and
    # the unmarked s2 migrated.
    assert _dois_of(store, survivor) == ["10.9/other"]
    assert ids["s2"] == "b" * 40

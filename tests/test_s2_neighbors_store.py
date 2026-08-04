"""Store-level CRUD tests for ``s2_neighbors`` (migration 0106,
paper-viewer-nav slice 3): the persisted Semantic Scholar neighbour list a
paper's Sources/Cited tabs read. Covers
:meth:`Store.replace_s2_neighbors` / :meth:`Store.list_s2_neighbors` /
:meth:`Store.s2_neighbors_fresh` directly, against an ephemeral migrated
postgres (the ``store`` fixture applies all migrations, so this also pins
that migration 0106 actually applies)."""

from __future__ import annotations

from precis.store import Store


def _paper(store: Store, slug: str) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=f"Paper {slug}")
    return int(ref.id)


def test_list_s2_neighbors_empty_when_never_written(store: Store) -> None:
    rid = _paper(store, "wang")
    assert store.list_s2_neighbors(rid, "cites") == []
    assert store.list_s2_neighbors(rid, "cited_by") == []


def test_s2_neighbors_fresh_false_when_no_rows(store: Store) -> None:
    rid = _paper(store, "wang")
    assert store.s2_neighbors_fresh(rid) is False


def test_replace_s2_neighbors_persists_and_orders(store: Store) -> None:
    rid = _paper(store, "wang")
    held = _paper(store, "kumar")
    n = store.replace_s2_neighbors(
        rid,
        "cites",
        [
            {
                "s2_id": "S2A",
                "doi": None,
                "title": "First",
                "year": 2019,
                "held_ref_id": None,
            },
            {
                "s2_id": "S2B",
                "doi": "10.1/x",
                "title": "Second",
                "year": 2020,
                "held_ref_id": held,
            },
        ],
    )
    assert n == 2

    rows = store.list_s2_neighbors(rid, "cites")
    assert [r.ord for r in rows] == [0, 1]
    assert rows[0].s2_id == "S2A"
    assert rows[0].held_ref_id is None
    assert rows[1].s2_id == "S2B"
    assert rows[1].held_ref_id == held
    assert rows[1].title == "Second"
    assert rows[1].year == 2020

    # the other direction is untouched
    assert store.list_s2_neighbors(rid, "cited_by") == []
    assert store.s2_neighbors_fresh(rid) is True


def test_replace_s2_neighbors_refresh_replaces_no_dup_rows(store: Store) -> None:
    rid = _paper(store, "wang")
    store.replace_s2_neighbors(
        rid,
        "cites",
        [
            {
                "s2_id": "S2A",
                "doi": None,
                "title": "A",
                "year": 2019,
                "held_ref_id": None,
            },
            {
                "s2_id": "S2B",
                "doi": None,
                "title": "B",
                "year": 2020,
                "held_ref_id": None,
            },
        ],
    )
    # second fetch: B dropped, C added, A reordered to position 1
    store.replace_s2_neighbors(
        rid,
        "cites",
        [
            {
                "s2_id": "S2C",
                "doi": None,
                "title": "C",
                "year": 2021,
                "held_ref_id": None,
            },
            {
                "s2_id": "S2A",
                "doi": None,
                "title": "A",
                "year": 2019,
                "held_ref_id": None,
            },
        ],
    )
    rows = store.list_s2_neighbors(rid, "cites")
    assert [r.s2_id for r in rows] == ["S2C", "S2A"]  # B is gone, no dup A


def test_replace_s2_neighbors_empty_list_clears(store: Store) -> None:
    rid = _paper(store, "wang")
    store.replace_s2_neighbors(
        rid,
        "cites",
        [
            {
                "s2_id": "S2A",
                "doi": None,
                "title": "A",
                "year": 2019,
                "held_ref_id": None,
            }
        ],
    )
    assert store.list_s2_neighbors(rid, "cites") != []
    store.replace_s2_neighbors(rid, "cites", [])
    assert store.list_s2_neighbors(rid, "cites") == []


def test_replace_s2_neighbors_both_directions_independent(store: Store) -> None:
    rid = _paper(store, "wang")
    store.replace_s2_neighbors(
        rid,
        "cites",
        [
            {
                "s2_id": "S2A",
                "doi": None,
                "title": "A",
                "year": 2019,
                "held_ref_id": None,
            }
        ],
    )
    store.replace_s2_neighbors(
        rid,
        "cited_by",
        [
            {
                "s2_id": "S2X",
                "doi": None,
                "title": "X",
                "year": 2022,
                "held_ref_id": None,
            },
            {
                "s2_id": "S2Y",
                "doi": None,
                "title": "Y",
                "year": 2023,
                "held_ref_id": None,
            },
        ],
    )
    assert [r.s2_id for r in store.list_s2_neighbors(rid, "cites")] == ["S2A"]
    assert [r.s2_id for r in store.list_s2_neighbors(rid, "cited_by")] == [
        "S2X",
        "S2Y",
    ]


def test_replace_s2_neighbors_cascades_on_ref_delete(store: Store) -> None:
    rid = _paper(store, "wang")
    store.replace_s2_neighbors(
        rid,
        "cites",
        [
            {
                "s2_id": "S2A",
                "doi": None,
                "title": "A",
                "year": 2019,
                "held_ref_id": None,
            }
        ],
    )
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM refs WHERE ref_id = %s", (rid,))
        conn.commit()
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM s2_neighbors WHERE ref_id = %s", (rid,)
        ).fetchone()
    assert row is not None
    assert row[0] == 0


def test_update_s2_neighbor_held_matches_by_s2_id_either_direction(
    store: Store,
) -> None:
    """A single-row Fetch (``/papers/{id}/fetch-ref``) stamps ``held_ref_id``
    on every neighbour row carrying the fetched ``s2_id`` — cites AND
    cited_by both flip, since it's the same external paper either way."""
    rid = _paper(store, "wang")
    store.replace_s2_neighbors(
        rid,
        "cites",
        [
            {
                "s2_id": "S2A",
                "doi": None,
                "title": "A",
                "year": 2019,
                "held_ref_id": None,
            }
        ],
    )
    store.replace_s2_neighbors(
        rid,
        "cited_by",
        [
            {
                "s2_id": "S2A",
                "doi": None,
                "title": "A",
                "year": 2019,
                "held_ref_id": None,
            }
        ],
    )
    new_stub = _paper(store, "a2019")
    n = store.update_s2_neighbor_held(rid, new_stub, s2_id="S2A")
    assert n == 2
    assert store.list_s2_neighbors(rid, "cites")[0].held_ref_id == new_stub
    assert store.list_s2_neighbors(rid, "cited_by")[0].held_ref_id == new_stub


def test_update_s2_neighbor_held_matches_by_doi(store: Store) -> None:
    rid = _paper(store, "wang")
    store.replace_s2_neighbors(
        rid,
        "cites",
        [
            {
                "s2_id": None,
                "doi": "10.1/x",
                "title": "A",
                "year": 2019,
                "held_ref_id": None,
            }
        ],
    )
    new_stub = _paper(store, "x2019")
    n = store.update_s2_neighbor_held(rid, new_stub, doi="10.1/x")
    assert n == 1
    assert store.list_s2_neighbors(rid, "cites")[0].held_ref_id == new_stub


def test_update_s2_neighbor_held_no_identifier_is_noop(store: Store) -> None:
    rid = _paper(store, "wang")
    store.replace_s2_neighbors(
        rid,
        "cites",
        [
            {
                "s2_id": "S2A",
                "doi": None,
                "title": "A",
                "year": 2019,
                "held_ref_id": None,
            }
        ],
    )
    assert store.update_s2_neighbor_held(rid, 999) == 0
    assert store.list_s2_neighbors(rid, "cites")[0].held_ref_id is None


def test_held_ref_id_set_null_when_held_ref_deleted(store: Store) -> None:
    rid = _paper(store, "wang")
    held = _paper(store, "kumar")
    store.replace_s2_neighbors(
        rid,
        "cites",
        [
            {
                "s2_id": "S2B",
                "doi": None,
                "title": "B",
                "year": 2020,
                "held_ref_id": held,
            }
        ],
    )
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM refs WHERE ref_id = %s", (held,))
        conn.commit()
    rows = store.list_s2_neighbors(rid, "cites")
    assert len(rows) == 1
    assert rows[0].held_ref_id is None  # ON DELETE SET NULL, row itself survives

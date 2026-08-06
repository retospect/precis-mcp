"""Store-level read test for ``paper_bib_entries`` (migration 0108,
citation-bib-parse) via :meth:`Store.list_bib_entries` (citation-sources-
tab) — against an ephemeral migrated postgres (the ``store`` fixture
applies all migrations, so this also pins that migration 0108 actually
applies). ``bib_parse`` (the worker pass) is the only writer; there's no
store-level insert accessor for this table, so rows are inserted directly
here the same way the worker does."""

from __future__ import annotations

from precis.store import Store


def _paper(store: Store, slug: str) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=f"Paper {slug}")
    return int(ref.id)


def _insert_entry(
    store: Store,
    ref_id: int,
    *,
    marker: int,
    raw_text: str = "raw citation text",
    authors: str | None = None,
    journal: str | None = None,
    year: int | None = None,
    volume: str | None = None,
    first_page: str | None = None,
    doi: str | None = None,
    s2_id: str | None = None,
    held_ref_id: int | None = None,
    parse_conf: float | None = 0.9,
    match_conf: float | None = None,
    parse_version: int = 1,
) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO paper_bib_entries "
            "(ref_id, marker, raw_text, authors, journal, year, volume, "
            "first_page, doi, s2_id, held_ref_id, parse_conf, match_conf, "
            "parse_version) "
            "VALUES (%(ref_id)s, %(marker)s, %(raw_text)s, %(authors)s, "
            "%(journal)s, %(year)s, %(volume)s, %(first_page)s, %(doi)s, "
            "%(s2_id)s, %(held_ref_id)s, %(parse_conf)s, %(match_conf)s, "
            "%(parse_version)s)",
            {
                "ref_id": ref_id,
                "marker": marker,
                "raw_text": raw_text,
                "authors": authors,
                "journal": journal,
                "year": year,
                "volume": volume,
                "first_page": first_page,
                "doi": doi,
                "s2_id": s2_id,
                "held_ref_id": held_ref_id,
                "parse_conf": parse_conf,
                "match_conf": match_conf,
                "parse_version": parse_version,
            },
        )
        conn.commit()


def test_list_bib_entries_empty_when_never_parsed(store: Store) -> None:
    rid = _paper(store, "wang")
    assert store.list_bib_entries(rid) == []


def test_list_bib_entries_orders_by_marker_and_maps_fields(store: Store) -> None:
    rid = _paper(store, "wang")
    held = _paper(store, "kumar")
    _insert_entry(
        store,
        rid,
        marker=34,
        raw_text="- [34] Z. Ali, ChemCatChem 2020, 12, 360.",
        authors="Ali, Z.",
        journal="ChemCatChem",
        year=2020,
        volume="12",
        first_page="360",
        doi="10.1/ali",
        s2_id="S2ALI",
        held_ref_id=held,
    )
    _insert_entry(store, rid, marker=5, raw_text="- [5] earlier entry")

    rows = store.list_bib_entries(rid)
    # ordered by marker ascending, not insertion order
    assert [r.marker for r in rows] == [5, 34]

    row = rows[1]
    assert row.ref_id == rid
    assert row.raw_text == "- [34] Z. Ali, ChemCatChem 2020, 12, 360."
    assert row.authors == "Ali, Z."
    assert row.journal == "ChemCatChem"
    assert row.year == 2020
    assert row.volume == "12"
    assert row.first_page == "360"
    assert row.doi == "10.1/ali"
    assert row.s2_id == "S2ALI"
    assert row.held_ref_id == held


def test_list_bib_entries_scoped_to_one_ref(store: Store) -> None:
    rid_a = _paper(store, "wang")
    rid_b = _paper(store, "kumar")
    _insert_entry(store, rid_a, marker=1)
    _insert_entry(store, rid_b, marker=1)
    assert len(store.list_bib_entries(rid_a)) == 1
    assert len(store.list_bib_entries(rid_b)) == 1


def test_list_bib_entries_cascades_on_ref_delete(store: Store) -> None:
    rid = _paper(store, "wang")
    _insert_entry(store, rid, marker=1)
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM refs WHERE ref_id = %s", (rid,))
        conn.commit()
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM paper_bib_entries WHERE ref_id = %s", (rid,)
        ).fetchone()
    assert row is not None
    assert row[0] == 0


def test_list_bib_entries_held_ref_id_set_null_when_held_ref_deleted(
    store: Store,
) -> None:
    rid = _paper(store, "wang")
    held = _paper(store, "kumar")
    _insert_entry(store, rid, marker=1, doi="10.1/x", held_ref_id=held)
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM refs WHERE ref_id = %s", (held,))
        conn.commit()
    rows = store.list_bib_entries(rid)
    assert len(rows) == 1
    assert rows[0].held_ref_id is None  # ON DELETE SET NULL, row itself survives

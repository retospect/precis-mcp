"""``precis.taproot.resolve.resolve_citation`` — the shared inline-marker
resolution API (docs/backlog/citation-taproot-resolve.md, AC 2).

Real DB ``store`` fixture. A paper's parsed bib entry is seeded directly
into ``paper_bib_entries`` (held / not-held / with DOI), a body chunk +
``chunk_citations`` row is seeded, and ``resolve_citation`` is asserted to
return the right identity — including ``held_ref_id`` when we hold the
cited paper.
"""

from __future__ import annotations

from typing import Any

from precis.store.types import ChunkInsert
from precis.taproot.resolve import BibResolution, resolve_citation


def _seed_paper(store: Any, *, slug: str) -> int:
    return int(store.insert_ref(kind="paper", slug=slug, title=f"P {slug}", meta={}).id)


def _seed_chunk(store: Any, ref_id: int, text: str) -> int:
    store.chunks.insert_chunks(ref_id, [ChunkInsert(ord=0, text=text, meta={})])
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = 0", (ref_id,)
        ).fetchone()
    assert row is not None
    return int(row[0])


def _seed_entry(
    store: Any,
    ref_id: int,
    marker: int,
    *,
    doi: str | None = None,
    held_ref_id: int | None = None,
    journal: str | None = None,
    year: int | None = None,
) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO paper_bib_entries "
            "(ref_id, marker, raw_text, journal, year, doi, held_ref_id, "
            " parse_version) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 1) RETURNING id",
            (ref_id, marker, f"raw {marker}", journal, year, doi, held_ref_id),
        ).fetchone()
        conn.commit()
    assert row is not None
    return int(row[0])


def _seed_citation(store: Any, chunk_id: int, marker: int, bib_entry_id: int) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_citations (chunk_id, marker, bib_entry_id) "
            "VALUES (%s, %s, %s)",
            (chunk_id, marker, bib_entry_id),
        )
        conn.commit()


def test_resolve_held_citation_returns_identity(store: Any) -> None:
    citing = _seed_paper(store, slug="res-citing")
    cited = _seed_paper(store, slug="res-cited")
    entry = _seed_entry(
        store,
        citing,
        126,
        doi="10.1002/cctc.202000001",
        held_ref_id=cited,
        journal="ChemCatChem",
        year=2020,
    )
    chunk = _seed_chunk(store, citing, "the effect was shown [126].")
    _seed_citation(store, chunk, 126, entry)

    res = resolve_citation(store, chunk, 126)
    assert isinstance(res, BibResolution)
    assert res.bib_entry_id == entry
    assert res.citing_ref_id == citing
    assert res.marker == 126
    assert res.held_ref_id == cited
    assert res.is_held is True
    assert res.doi == "10.1002/cctc.202000001"
    assert res.journal == "ChemCatChem"
    assert res.year == 2020


def test_resolve_not_held_citation_has_doi_but_no_held_ref(store: Any) -> None:
    citing = _seed_paper(store, slug="res-notheld")
    entry = _seed_entry(store, citing, 42, doi="10.9999/unheld", held_ref_id=None)
    chunk = _seed_chunk(store, citing, "as reported [42].")
    _seed_citation(store, chunk, 42, entry)

    res = resolve_citation(store, chunk, 42)
    assert res is not None
    assert res.held_ref_id is None
    assert res.is_held is False
    assert res.doi == "10.9999/unheld"


def test_resolve_unknown_marker_returns_none(store: Any) -> None:
    citing = _seed_paper(store, slug="res-none")
    entry = _seed_entry(store, citing, 5)
    chunk = _seed_chunk(store, citing, "single cite [5].")
    _seed_citation(store, chunk, 5, entry)

    # No chunk_citations row for marker 6 in this chunk.
    assert resolve_citation(store, chunk, 6) is None

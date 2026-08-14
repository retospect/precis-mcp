"""bib_mark sweep — inline citation markers → ``chunk_citations``
(docs/backlog/citation-taproot-resolve.md, AC 1).

Real DB ``store`` fixture. A paper's parsed bibliography is seeded directly
into ``paper_bib_entries`` (the base slice), body chunks with inline
markers are inserted, and the sweep is driven end-to-end — asserting the
false-positive guard (only real bib markers accepted), range/comma
expansion, ``<sup>`` handling, the ``BIBMARK`` done-marker convergence, and
version-bump re-sweep.
"""

from __future__ import annotations

from typing import Any

import precis.workers.bib_mark as bib_mark
from precis.store.types import BlockInsert
from precis.workers.bib_mark import BIBMARK_VERSION, _extract_markers, run_bib_mark_pass

# ── _extract_markers (unit) ──────────────────────────────────────────


class TestExtractMarkers:
    def test_single_marker(self) -> None:
        assert _extract_markers("as shown [126].", frozenset({126, 127})) == {126}

    def test_comma_group_expands(self) -> None:
        assert _extract_markers("[129,130]", frozenset({129, 130})) == {129, 130}

    def test_hyphen_range_expands(self) -> None:
        assert _extract_markers("[126-128]", frozenset({126, 127, 128})) == {
            126,
            127,
            128,
        }

    def test_en_dash_range_expands(self) -> None:
        assert _extract_markers("[126–128]", frozenset({126, 127, 128})) == {
            126,
            127,
            128,
        }

    def test_sup_wrapped_marker(self) -> None:
        assert _extract_markers("effect<sup>[126]</sup>", frozenset({126})) == {126}

    def test_number_above_max_marker_rejected(self) -> None:
        # False-positive guard: 999 isn't a real bib marker for the paper.
        assert _extract_markers("figure [999]", frozenset({126, 127})) == set()

    def test_incidental_bracketed_number_rejected(self) -> None:
        # A prose paper mentioning "[3]" whose bibliography has no marker 3.
        assert _extract_markers("as earlier [3]", frozenset({126})) == set()

    def test_letters_in_brackets_never_match(self) -> None:
        assert _extract_markers("[H2O] and [see 12]", frozenset({12})) == set()

    def test_absurd_range_skipped_whole(self) -> None:
        # A page-span-shaped range wider than the cap is not a citation.
        assert _extract_markers("[1-9000]", frozenset({1, 5, 42})) == set()


# ── seeding helpers ──────────────────────────────────────────────────


def _seed_paper(store: Any, *, slug: str) -> int:
    return int(store.insert_ref(kind="paper", slug=slug, title=f"P {slug}", meta={}).id)


def _seed_entry(store: Any, ref_id: int, marker: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO paper_bib_entries (ref_id, marker, raw_text, parse_version) "
            "VALUES (%s, %s, %s, 1) RETURNING id",
            (ref_id, marker, f"entry {marker}"),
        ).fetchone()
        conn.commit()
    assert row is not None
    return int(row[0])


def _seed_chunk(store: Any, ref_id: int, ord_: int, text: str) -> int:
    store.blocks.insert_blocks(ref_id, [BlockInsert(pos=ord_, text=text, meta={})])
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = %s", (ref_id, ord_)
        ).fetchone()
    assert row is not None
    return int(row[0])


def _citations(store: Any, chunk_id: int) -> list[tuple[int, int]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT marker, bib_entry_id FROM chunk_citations "
            "WHERE chunk_id = %s ORDER BY marker",
            (chunk_id,),
        ).fetchall()
    return [(int(r[0]), int(r[1])) for r in rows]


def _bibmark_tagged(store: Any, chunk_id: int, version: str = BIBMARK_VERSION) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM chunk_tags ct JOIN tags t USING (tag_id) "
            "WHERE ct.chunk_id = %s AND t.namespace = 'BIBMARK' AND t.value = %s",
            (chunk_id, version),
        ).fetchone()
    return row is not None


# ── the sweep (integration, AC 1) ────────────────────────────────────


def test_worked_example_markers_and_expansion(store: Any) -> None:
    paper = _seed_paper(store, slug="bm-worked")
    e126 = _seed_entry(store, paper, 126)
    e127 = _seed_entry(store, paper, 127)
    e129 = _seed_entry(store, paper, 129)
    e130 = _seed_entry(store, paper, 130)
    chunk = _seed_chunk(
        store,
        paper,
        0,
        "The catalyst outperforms prior work [126] and later studies [127]; "
        "see also [129,130]. Figure [999] is unrelated.",
    )

    result = run_bib_mark_pass(store, batch_size=50)
    assert result["chunks_swept"] == 1
    assert result["failed"] == 0

    # 126, 127, and the expanded 129 + 130 — but NOT 999 (above max marker).
    assert _citations(store, chunk) == [
        (126, e126),
        (127, e127),
        (129, e129),
        (130, e130),
    ]
    assert _bibmark_tagged(store, chunk) is True


def test_swept_chunk_not_reclaimed_at_same_version(store: Any) -> None:
    paper = _seed_paper(store, slug="bm-idem")
    _seed_entry(store, paper, 5)
    chunk = _seed_chunk(store, paper, 0, "prior result [5].")

    first = run_bib_mark_pass(store, batch_size=50)
    assert first["chunks_swept"] == 1
    assert _citations(store, chunk) == [(5, _citations(store, chunk)[0][1])]

    # Same version: converged, nothing re-claimed.
    second = run_bib_mark_pass(store, batch_size=50)
    assert second["chunks_swept"] == 0


def test_chunk_with_no_valid_markers_is_swept_but_writes_nothing(store: Any) -> None:
    """Convergence: a body chunk whose only bracketed number isn't a bib
    marker is still marked swept (never re-probed) but writes no rows."""
    paper = _seed_paper(store, slug="bm-empty")
    _seed_entry(store, paper, 12)
    chunk = _seed_chunk(store, paper, 0, "unrelated figure [999] reference only.")

    result = run_bib_mark_pass(store, batch_size=50)
    assert result["chunks_swept"] == 1
    assert _citations(store, chunk) == []
    assert _bibmark_tagged(store, chunk) is True


def test_paper_without_bib_entries_is_not_swept(store: Any) -> None:
    paper = _seed_paper(store, slug="bm-nobib")
    chunk = _seed_chunk(store, paper, 0, "text with [1] a marker but no parsed bib.")

    result = run_bib_mark_pass(store, batch_size=50)
    assert result["chunks_swept"] == 0
    assert _citations(store, chunk) == []


def test_version_bump_resweeps(store: Any, monkeypatch) -> None:
    paper = _seed_paper(store, slug="bm-bump")
    _seed_entry(store, paper, 7)
    chunk = _seed_chunk(store, paper, 0, "a claim [7].")

    first = run_bib_mark_pass(store, batch_size=50)
    assert first["chunks_swept"] == 1
    assert _bibmark_tagged(store, chunk, "1") is True

    # Bump the sweep version: the chunk (tagged v1) is re-claimed and its
    # citations re-written under v2.
    monkeypatch.setattr(bib_mark, "BIBMARK_VERSION", "2")
    second = run_bib_mark_pass(store, batch_size=50)
    assert second["chunks_swept"] == 1
    assert _bibmark_tagged(store, chunk, "2") is True
    assert _citations(store, chunk) == [(7, _citations(store, chunk)[0][1])]

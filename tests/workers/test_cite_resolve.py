"""cite_resolve — chunk→paper ``cites`` links from inline markers
(docs/backlog/citation-resolution-skill.md, gr513768 slice 1).

Real DB ``store`` fixture. ``chunk_citations`` rows come from the real
``bib_mark`` sweep over seeded ``paper_bib_entries``; the citing paper
is marked ``CITERES:pending`` by hand (the first-``get`` trigger is
slice 5), then ``run_cite_resolve_pass`` writes the chunk→paper links
— asserting ``meta.marker``/``marker_order`` labels and ordinals,
occurrence-order rendering, idempotent re-runs, the unheld-entry skip,
the ``pending → swept`` tag flip, the bib_mark-swept gate, and the
per-paper savepoint isolation + ``CITERES:error`` backoff.
"""

from __future__ import annotations

from typing import Any

import pytest

import precis.workers.cite_resolve as cite_resolve
from precis.store.types import ChunkInsert, Tag
from precis.workers.bib_mark import run_bib_mark_pass
from precis.workers.cite_resolve import (
    CITERES_VERSION,
    BibEntry,
    _author_year_index,
    _ordered_occurrences,
    run_cite_resolve_pass,
)
from tests.factories import seed_paper_ref

_NO_AY: dict[Any, Any] = {}


def _seed_entry(
    store: Any,
    ref_id: int,
    marker: int,
    held_ref_id: int | None = None,
    *,
    authors: str | None = None,
    year: int | None = None,
) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO paper_bib_entries "
            "(ref_id, marker, raw_text, authors, year, held_ref_id, "
            "parse_version) VALUES (%s, %s, %s, %s, %s, %s, 1) RETURNING id",
            (ref_id, marker, f"entry {marker}", authors, year, held_ref_id),
        ).fetchone()
        conn.commit()
    assert row is not None
    return int(row[0])


def _seed_chunk(store: Any, ref_id: int, ord_: int, text: str) -> int:
    store.chunks.insert_chunks(ref_id, [ChunkInsert(ord=ord_, text=text, meta={})])
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = %s",
            (ref_id, ord_),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _mark_pending(store: Any, ref_id: int) -> None:
    store.add_tag(ref_id, Tag.closed("CITERES", "pending"), set_by="system")


def _cites_links(store: Any, ref_id: int) -> list[Any]:
    return store.links_for(ref_id, direction="out", relation="cites")


def _citeres_tagged(store: Any, chunk_id: int, version: str = CITERES_VERSION) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM chunk_tags ct JOIN tags t USING (tag_id) "
            "WHERE ct.chunk_id = %s AND t.namespace = 'CITERES' AND t.value = %s",
            (chunk_id, version),
        ).fetchone()
    return row is not None


# ── _ordered_occurrences (unit) ──────────────────────────────────────


class TestOrderedOccurrences:
    def test_occurrence_order_by_text_position(self) -> None:
        m2e = {5: 50, 3: 30, 4: 40}
        ordered = _ordered_occurrences("prior [5] and [3-4] here.", m2e, _NO_AY)
        assert ordered == [(0, "5", 50), (1, "[3-4]", 30), (2, "[3-4]", 40)]

    def test_repeated_marker_keeps_first_position(self) -> None:
        m2e = {5: 50, 7: 70}
        ordered = _ordered_occurrences("[5] then [7] and again [5].", m2e, _NO_AY)
        assert ordered == [(0, "5", 50), (1, "7", 70)]

    def test_comma_group_label(self) -> None:
        m2e = {7: 70, 9: 90}
        ordered = _ordered_occurrences("both [7,9] hold.", m2e, _NO_AY)
        assert ordered == [(0, "[7,9]", 70), (1, "[7,9]", 90)]

    def test_marker_missing_from_text_still_links_last(self) -> None:
        m2e = {5: 50, 8: 80}
        ordered = _ordered_occurrences("only [5] appears.", m2e, _NO_AY)
        assert ordered == [(0, "5", 50), (1, "8", 80)]

    def test_mixed_numeric_and_author_year_order_by_position(self) -> None:
        ay = _author_year_index(
            [
                BibEntry(
                    id=99, marker=2, authors="Miller, A.", year=1921, held_ref_id=None
                )
            ]
        )
        ordered = _ordered_occurrences(
            "numeric [5] then Miller (1921) then [7].", {5: 50, 7: 70}, ay
        )
        assert ordered == [
            (0, "5", 50),
            (1, "Miller 1921", 99),
            (2, "7", 70),
        ]


# ── author-year fixture (slice 2: ≥30 labelled sentences) ───────────

#: The fixture bibliography — one paper's ``paper_bib_entries`` (id =
#: marker for readability). ``held_ref_id`` values are stand-ins; the
#: match tests exercise ``_author_year_index`` + ``_ordered_occurrences``
#: without a DB.
_AY_ENTRIES = [
    BibEntry(1, 1, "Miller, A. B.; Jones, C. D.", 1921, 9001),
    BibEntry(2, 2, "Smith, J.; Brown, K.", 2004, 9002),
    BibEntry(3, 3, "Smith, J.; Lee, R.", 2004, 9003),  # Smith-2004 pair → ambiguous
    BibEntry(4, 4, "Garcia-Lopez, M.", 2019, 9004),
    BibEntry(5, 5, "O'Brien, P.", 1999, 9005),
    BibEntry(6, 6, "van der Waals, J. D.", 1873, 9006),
    BibEntry(7, 7, "Kim, S.; Park, J.", 2015, 9007),
    BibEntry(8, 8, "Chen, L.", 2010, 9008),
    BibEntry(9, 9, "Müller, H.", 1987, 9009),
    BibEntry(10, 10, "Dupont, F.", 1978, None),  # matched but unheld → no link
    BibEntry(11, 11, "Zhang, W.", 2005, 9011),
    BibEntry(12, 12, "Anonymous", None, 9012),  # yearless → never indexed
    BibEntry(13, 13, "A. B. Einstein and C. D. Fermi", 1905, 9013),
]

#: (sentence, entry ids the author-year layer must resolve). ``()`` =
#: no link — the unmatched and ambiguous cases precision tolerates but
#: correctness forbids getting wrong.
_AY_FIXTURE: list[tuple[str, set[int]]] = [
    ("As shown by Miller (1921), the effect persists.", {1}),
    ("The baseline (Miller, 1921) still stands.", {1}),
    ("Miller 1921 first reported it.", {1}),
    ("Kim and Park (2015) demonstrated the mechanism.", {7}),
    ("(Kim and Park, 2015) is the standard reference.", {7}),
    ("Kim & Park (2015) reported similar.", {7}),
    ("The seminal result (Kim et al., 2015) holds.", {7}),
    ("Kim et al. (2015) confirmed it.", {7}),
    ("Garcia-Lopez (2019) extended the model.", {4}),
    ("This mirrors earlier work (Garcia-Lopez, 2019).", {4}),
    ("O'Brien (1999) argued otherwise.", {5}),
    ("van der Waals (1873) derived the equation.", {6}),
    ("The force law (van der Waals, 1873) follows.", {6}),
    ("Chen (2010) describes the setup.", {8}),
    ("Later work Chen 2010 confirmed.", {8}),
    ("Zhang (2005a) is the first variant.", {11}),
    ("The second variant (Zhang, 2005b) differs.", {11}),
    ("Müller (1987) predicted this.", {9}),
    ("Einstein and Fermi (1905) proposed it.", {13}),
    ("Multiple studies (Miller, 1921; Chen, 2010) converge.", {1, 8}),
    ("see also Miller and Jones (1921).", {1}),
    # particle lookbehind: "der"/"de" inside a preceding word is not a
    # nobiliary particle — the surname alone must resolve.
    ("We consider Miller (1921) the seminal work.", {1}),
    ("The regime under Kim and Park (2015) held.", {7}),
    ("The observations made Chen (2010) show this.", {8}),
    # unmatched / ambiguous — no link may be written
    ("Smith (2004) is cited here.", set()),  # ambiguous: entries 2 + 3
    ("(Smith et al., 2004) is well known.", set()),  # ambiguous
    ("Smith and Lee (2004) is the other study.", set()),  # ambiguous
    ("Jones (2010) reports nothing.", set()),  # Jones is 2nd author of 1
    ("Brown (2004) alone.", set()),  # 2nd author of 2
    ("Park (2015) reviewed it.", set()),  # 2nd author of 7
    ("In 2004, the reaction was studied.", set()),  # "In" isn't a bib surname
    ("Figure 1921 shows the apparatus.", set()),
    ("Dupont (1978) is unheld.", {10}),  # resolves; unheld → writes nothing
    ("The year 2004 saw advances.", set()),
    ("Miller (1922) disagreed.", set()),  # year off by one
    ("Section 2004a collects the data.", set()),
    ("Anonymous (1977) said so.", set()),  # entry 12 has no year
    ("We report proteins (H2O, CH4), no citations.", set()),
]


def _ay_resolve(text: str) -> list[tuple[int, str, int]]:
    return _ordered_occurrences(text, {}, _author_year_index(_AY_ENTRIES))


#: The held filter the pass applies at write time: resolving to an
#: unheld entry is correct — the pass just writes no link for it — so
#: precision is scored on the *writable* set (resolved ∩ held), and
#: each row's expected ids pin the raw resolution separately.
_HELD_IDS = {e.id for e in _AY_ENTRIES if e.held_ref_id is not None}


def test_author_year_fixture_precision() -> None:
    """≥30 labelled sentences: precision ≥ 0.95 (unmatched is fine,
    wrong is not — spec slice-2 acceptance). Precision is scored after
    the held filter — the Dupont row resolves to entry 10 (expected
    ``{10}``) but ``held_ref_id`` is NULL, so it is not a wrong write."""
    assert len(_AY_FIXTURE) >= 30
    written = 0
    correct = 0
    missed: list[str] = []
    for text, expected in _AY_FIXTURE:
        resolved = {entry_id for _, _, entry_id in _ay_resolve(text)}
        got = resolved & _HELD_IDS
        written += len(got)
        correct += len(got & expected)
        if resolved != expected:
            missed.append(
                f"{text!r}: resolved {sorted(resolved)}, want {sorted(expected)}"
            )
    precision = correct / written if written else 1.0
    assert precision >= 0.95, f"precision {precision:.2f}; misses: {missed}"
    # recall sanity: the fixture's positives should mostly resolve —
    # catching a regex regression that silently matches nothing.
    assert len(missed) <= 2, missed


def test_author_year_surname_extraction() -> None:
    index = _author_year_index(_AY_ENTRIES)
    assert ("miller", 1921) in index
    assert ("garcia-lopez", 2019) in index
    assert ("o'brien", 1999) in index
    assert ("vanderwaals", 1873) in index  # spaces fold away in _norm_name
    assert ("einstein", 1905) in index  # non-ACS "A. B. Einstein and ..."
    # the Smith-2004 pair stays ambiguous
    assert len(index[("smith", 2004)]) == 2


# ── the pass (integration, slice-1 acceptance) ───────────────────────


def test_fixture_paper_markers_write_ordered_links(store: Any) -> None:
    cited_a = seed_paper_ref(store, slug="cr-cited-a")
    cited_b = seed_paper_ref(store, slug="cr-cited-b")
    cited_c = seed_paper_ref(store, slug="cr-cited-c")
    paper = seed_paper_ref(store, slug="cr-citing")
    e3 = _seed_entry(store, paper, 3, cited_a)
    e4 = _seed_entry(store, paper, 4, cited_b)
    e5 = _seed_entry(store, paper, 5, cited_c)
    _seed_entry(store, paper, 9)  # unheld — writes nothing
    chunk = _seed_chunk(
        store,
        paper,
        0,
        "As shown before [5], with extensions [3-4]; cf. also [9] and again [5].",
    )
    run_bib_mark_pass(store, batch_size=50)
    _mark_pending(store, paper)

    result = run_cite_resolve_pass(store, batch_size=10)
    assert result["papers_swept"] == 1
    assert result["chunks_swept"] == 1
    assert result["links_ensured"] == 3
    assert result["failed"] == 0

    links = _cites_links(store, paper)
    assert len(links) == 3
    # links_for's ORDER BY created_at, link_id preserves the insertion
    # (occurrence) order — [5] first, then the [3-4] expansion.
    assert [
        (l.dst_ref_id, l.meta["marker"], l.meta["marker_order"]) for l in links
    ] == [
        (cited_c, "5", 0),
        (cited_a, "[3-4]", 1),
        (cited_b, "[3-4]", 2),
    ]
    by_dst = {l.dst_ref_id: l for l in links}
    assert by_dst[cited_c].meta["bib_entry_id"] == e5
    assert by_dst[cited_a].meta["bib_entry_id"] == e3
    assert by_dst[cited_b].meta["bib_entry_id"] == e4
    for link in links:
        assert link.set_by == "system"
        assert link.src_chunk_id == chunk
        assert link.dst_chunk_id is None
        assert link.meta["source"] == "cite_resolve"
        assert link.meta["writer"] == f"cite_resolve:{CITERES_VERSION}"

    # The unheld entry wrote nothing; the chunk is stamped swept and the
    # paper flipped pending -> swept.
    assert _citeres_tagged(store, chunk) is True
    assert store.has_tag(paper, "CITERES", "swept") is True
    assert store.has_tag(paper, "CITERES", "pending") is False

    # Re-run: converged — nothing claimed, nothing inserted.
    second = run_cite_resolve_pass(store, batch_size=10)
    assert second["papers_swept"] == 0
    assert len(_cites_links(store, paper)) == 3


def test_unmarked_paper_is_not_touched(store: Any) -> None:
    cited = seed_paper_ref(store, slug="cr-unmarked-cited")
    paper = seed_paper_ref(store, slug="cr-unmarked")
    _seed_entry(store, paper, 1, cited)
    _seed_chunk(store, paper, 0, "prior [1].")
    run_bib_mark_pass(store, batch_size=50)

    result = run_cite_resolve_pass(store, batch_size=10)
    assert result["papers_swept"] == 0
    assert _cites_links(store, paper) == []


def test_pending_paper_with_no_entries_still_swept(store: Any) -> None:
    paper = seed_paper_ref(store, slug="cr-noentries")
    _seed_chunk(store, paper, 0, "plain body text, no markers.")
    _mark_pending(store, paper)

    result = run_cite_resolve_pass(store, batch_size=10)
    assert result["papers_swept"] == 1
    assert _cites_links(store, paper) == []
    assert store.has_tag(paper, "CITERES", "swept") is True


def test_self_cite_and_retired_target_write_nothing(store: Any) -> None:
    paper = seed_paper_ref(store, slug="cr-self")
    retired = seed_paper_ref(store, slug="cr-retired")
    _seed_entry(store, paper, 1, paper)  # self-cite skipped
    _seed_entry(store, paper, 2, retired)
    _seed_chunk(store, paper, 0, "earlier [1] and later [2].")
    run_bib_mark_pass(store, batch_size=50)
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET retired_at = now() WHERE ref_id = %s", (retired,))
        conn.commit()
    _mark_pending(store, paper)

    result = run_cite_resolve_pass(store, batch_size=10)
    assert result["papers_swept"] == 1
    assert result["links_ensured"] == 0
    assert _cites_links(store, paper) == []


def test_author_year_writes_links_and_nothing_stored(store: Any) -> None:
    """Slice-2 acceptance: the author-year match leaves no stored row —
    no table, no ``chunk_citations`` row — only the ``cites`` link."""
    cited = seed_paper_ref(store, slug="cr-ay-cited")
    paper = seed_paper_ref(store, slug="cr-ay-citing")
    _seed_entry(store, paper, 1, cited, authors="Miller, A. B.", year=1921)
    _seed_entry(store, paper, 2, None, authors="Dupont, F.", year=1978)
    chunk = _seed_chunk(
        store,
        paper,
        0,
        "As Miller (1921) showed, and Dupont (1978) confirmed, the phase holds.",
    )
    run_bib_mark_pass(store, batch_size=50)  # no [N] markers → no rows
    _mark_pending(store, paper)

    result = run_cite_resolve_pass(store, batch_size=10)
    assert result["links_ensured"] == 1  # Miller held; Dupont unheld → nothing

    links = _cites_links(store, paper)
    assert len(links) == 1
    link = links[0]
    assert link.dst_ref_id == cited
    assert link.src_chunk_id == chunk
    assert link.meta["marker"] == "Miller 1921"
    assert link.meta["marker_order"] == 0

    # Nothing stored for the author-year parse: chunk_citations stays empty.
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM chunk_citations WHERE chunk_id = %s",
            (chunk,),
        ).fetchone()
    assert row is not None and int(row[0]) == 0


def test_pending_paper_waits_for_bib_mark(store: Any) -> None:
    """Spec ruling: the pass requires bib_mark to have swept the paper
    first — a pending paper bib_mark hasn't swept is never claimed and
    stays ``CITERES:pending`` (not stamped swept)."""
    cited = seed_paper_ref(store, slug="cr-gated-cited")
    paper = seed_paper_ref(store, slug="cr-gated")
    _seed_entry(store, paper, 1, cited)
    chunk = _seed_chunk(store, paper, 0, "prior [1].")
    _mark_pending(store, paper)
    # No run_bib_mark_pass: the paper has entries but its chunks carry
    # no BIBMARK tag — cite_resolve must wait for the marker map.

    result = run_cite_resolve_pass(store, batch_size=10)
    assert result["papers_swept"] == 0
    assert _cites_links(store, paper) == []
    assert _citeres_tagged(store, chunk) is False
    assert store.has_tag(paper, "CITERES", "pending") is True
    assert store.has_tag(paper, "CITERES", "swept") is False

    # Once bib_mark has swept, the same pass drains it.
    run_bib_mark_pass(store, batch_size=50)
    result = run_cite_resolve_pass(store, batch_size=10)
    assert result["papers_swept"] == 1
    assert len(_cites_links(store, paper)) == 1
    assert store.has_tag(paper, "CITERES", "swept") is True


def test_unswept_pending_papers_skip_batch_slots(store: Any) -> None:
    """The bib_mark gate lives in the claim WHERE: ``batch_size``
    unswept pending papers + one ready paper — the ready one is swept
    because the waiters never take a slot."""
    cited = seed_paper_ref(store, slug="cr-slot-cited")
    waiters = [seed_paper_ref(store, slug=f"cr-slot-wait-{i}") for i in range(2)]
    for paper in waiters:
        # chunks but NO bib entries yet — bib_mark skips the paper, so
        # its chunks stay unmarked when the pass runs below.
        _seed_chunk(store, paper, 0, "prior [1].")
    ready = seed_paper_ref(store, slug="cr-slot-ready")
    _seed_entry(store, ready, 1, cited)
    _seed_chunk(store, ready, 0, "prior [1].")
    run_bib_mark_pass(store, batch_size=50)  # only `ready` qualifies
    for paper in waiters:
        _seed_entry(store, paper, 1, cited)  # now pending-but-unswept
        _mark_pending(store, paper)
    _mark_pending(store, ready)

    result = run_cite_resolve_pass(store, batch_size=len(waiters))
    assert result["papers_swept"] == 1
    assert store.has_tag(ready, "CITERES", "swept") is True
    for paper in waiters:
        assert store.has_tag(paper, "CITERES", "pending") is True
        assert store.has_tag(paper, "CITERES", "swept") is False

    run_bib_mark_pass(store, batch_size=50)
    result = run_cite_resolve_pass(store, batch_size=10)
    assert result["papers_swept"] == 2


def test_failing_paper_rolls_back_only_itself(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec ruling: per-paper isolation — each paper sweeps in a
    savepoint; a failing paper rolls back only itself, is tagged
    ``CITERES:error`` and backed off (not re-claimed first next pass);
    the rest of the batch commits."""
    cited = seed_paper_ref(store, slug="cr-iso-cited")
    bad = seed_paper_ref(store, slug="cr-iso-bad")
    good = seed_paper_ref(store, slug="cr-iso-good")
    for paper in (bad, good):
        _seed_entry(store, paper, 1, cited)
        _seed_chunk(store, paper, 0, "prior [1].")
    run_bib_mark_pass(store, batch_size=50)
    for paper in (bad, good):
        _mark_pending(store, paper)

    real = cite_resolve.ensure_chunk_cites_links

    def _boom(store_: Any, ref_id: int, *args: Any, **kwargs: Any) -> int:
        if ref_id == bad:
            raise RuntimeError("injected failure")
        return real(store_, ref_id, *args, **kwargs)

    monkeypatch.setattr(cite_resolve, "ensure_chunk_cites_links", _boom)

    result = run_cite_resolve_pass(store, batch_size=10)
    assert result["failed"] == 1
    assert result["papers_swept"] == 1
    # the good paper committed; the bad one rolled back entirely.
    assert store.has_tag(good, "CITERES", "swept") is True
    assert len(_cites_links(store, good)) == 1
    assert _cites_links(store, bad) == []
    assert store.has_tag(bad, "CITERES", "swept") is False
    assert store.has_tag(bad, "CITERES", "pending") is True
    assert store.has_tag(bad, "CITERES", "error:1") is True

    # Backoff: a clean pending paper at a HIGHER ref_id is claimed ahead
    # of the error-tagged one (batch_size=1 ⇒ the claim order shows it).
    clean = seed_paper_ref(store, slug="cr-iso-clean")
    _seed_entry(store, clean, 1, cited)
    _seed_chunk(store, clean, 0, "prior [1].")
    run_bib_mark_pass(store, batch_size=50)
    _mark_pending(store, clean)
    monkeypatch.undo()

    one = run_cite_resolve_pass(store, batch_size=1)
    assert one["papers_swept"] == 1
    assert store.has_tag(clean, "CITERES", "swept") is True
    assert store.has_tag(bad, "CITERES", "pending") is True

    # Retry succeeds — and the pending -> swept flip clears the error tag.
    retry = run_cite_resolve_pass(store, batch_size=10)
    assert retry["papers_swept"] == 1
    assert retry["failed"] == 0
    assert store.has_tag(bad, "CITERES", "swept") is True
    assert store.has_tag(bad, "CITERES", "error:1") is False
    assert len(_cites_links(store, bad)) == 1


def test_terminal_after_max_error_attempts(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retry cap: the third failure stamps ``error:terminal`` and the
    paper is never claimed again — even after the defect is gone."""
    cited = seed_paper_ref(store, slug="cr-term-cited")
    bad = seed_paper_ref(store, slug="cr-term-bad")
    _seed_entry(store, bad, 1, cited)
    _seed_chunk(store, bad, 0, "prior [1].")
    run_bib_mark_pass(store, batch_size=50)
    _mark_pending(store, bad)

    def _boom(store_: Any, ref_id: int, *args: Any, **kwargs: Any) -> int:
        raise RuntimeError("injected failure")

    monkeypatch.setattr(cite_resolve, "ensure_chunk_cites_links", _boom)

    for _ in range(3):
        result = run_cite_resolve_pass(store, batch_size=10)
        assert result["failed"] == 1
        assert result["papers_swept"] == 0
    assert store.has_tag(bad, "CITERES", "error:1") is True
    assert store.has_tag(bad, "CITERES", "error:2") is True
    assert store.has_tag(bad, "CITERES", "error:terminal") is True

    # Terminal: the claim excludes it outright — a clean pass touches
    # nothing even with the fault removed.
    monkeypatch.undo()
    result = run_cite_resolve_pass(store, batch_size=10)
    assert result["papers_swept"] == 0
    assert result["failed"] == 0
    assert store.has_tag(bad, "CITERES", "pending") is True
    assert store.has_tag(bad, "CITERES", "swept") is False
    assert _cites_links(store, bad) == []


def test_batch_rollback_recounts_and_restamps(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit-level rollback loses the batch's error stamps — the
    pass re-stamps them so the record survives, and ``failed`` counts
    each paper once (two paper failures + rollback = 2, not 3)."""
    import psycopg

    cited = seed_paper_ref(store, slug="cr-rb-cited")
    bad_a = seed_paper_ref(store, slug="cr-rb-bad-a")
    bad_b = seed_paper_ref(store, slug="cr-rb-bad-b")
    good = seed_paper_ref(store, slug="cr-rb-good")
    bads = {bad_a, bad_b}
    for paper in (bad_a, bad_b, good):
        _seed_entry(store, paper, 1, cited)
        _seed_chunk(store, paper, 0, "prior [1].")
    run_bib_mark_pass(store, batch_size=50)
    for paper in (bad_a, bad_b, good):
        _mark_pending(store, paper)

    real = cite_resolve.ensure_chunk_cites_links

    def _boom(store_: Any, ref_id: int, *args: Any, **kwargs: Any) -> int:
        if ref_id in bads:
            raise RuntimeError("injected failure")
        return real(store_, ref_id, *args, **kwargs)

    monkeypatch.setattr(cite_resolve, "ensure_chunk_cites_links", _boom)

    # The first commit of the run raises — the batch rolls back, taking
    # the two error stamps with it.
    real_commit = psycopg.Connection.commit
    commits = {"n": 0}

    def _flaky_commit(self: Any) -> None:
        commits["n"] += 1
        if commits["n"] == 1:
            raise RuntimeError("commit failed")
        real_commit(self)

    monkeypatch.setattr(psycopg.Connection, "commit", _flaky_commit)

    result = run_cite_resolve_pass(store, batch_size=10)
    assert result["failed"] == 2  # the rollback doesn't add a phantom third
    assert result["papers_swept"] == 0  # good's sweep rolled back too
    for bad in bads:
        # re-stamped in the fresh transaction after the rollback
        assert store.has_tag(bad, "CITERES", "error:1") is True
        assert store.has_tag(bad, "CITERES", "pending") is True
        assert store.has_tag(bad, "CITERES", "swept") is False
    assert store.has_tag(good, "CITERES", "swept") is False

    monkeypatch.undo()
    retry = run_cite_resolve_pass(store, batch_size=10)
    assert retry["papers_swept"] == 3
    assert retry["failed"] == 0

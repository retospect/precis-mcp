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
from precis.utils.rate_limit import RateLimited
from precis.workers import ref_lease
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


@pytest.fixture(autouse=True)
def _no_live_openalex(monkeypatch: pytest.MonkeyPatch) -> None:
    """Slice 4's live title lookup never touches the network here —
    the default fake finds nothing (a miss); slice-4 tests re-patch
    per case. The shared limiter row is equally never consulted."""
    monkeypatch.setattr(
        cite_resolve.openalex_meta, "search_openalex_works", lambda *a, **k: []
    )


def _seed_entry(
    store: Any,
    ref_id: int,
    marker: int,
    held_ref_id: int | None = None,
    *,
    authors: str | None = None,
    year: int | None = None,
    raw_text: str | None = None,
    doi: str | None = None,
    openalex_id: str | None = None,
    source: str = "bib_parse",
    match_conf: float | None = None,
) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO paper_bib_entries "
            "(ref_id, marker, raw_text, authors, year, held_ref_id, doi, "
            " openalex_id, source, match_conf, parse_version) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1) RETURNING id",
            (
                ref_id,
                marker,
                raw_text if raw_text is not None else f"entry {marker}",
                authors,
                year,
                held_ref_id,
                doi,
                openalex_id,
                source,
                match_conf,
            ),
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
    """Spec ruling: per-paper isolation — a failing paper rolls back
    only itself, is tagged ``CITERES:error`` and backed off (not
    re-claimed first next pass); the rest of the batch commits.
    ``ATTEMPT_COOLDOWN_MIN=0`` expires each attempt lease at once so
    the retry assertions aren't braked by the 60-min lease."""
    monkeypatch.setattr(ref_lease, "ATTEMPT_COOLDOWN_MIN", 0)
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
    paper is never claimed again — even after the defect is gone.
    ``ATTEMPT_COOLDOWN_MIN=0`` so the three attempts aren't spread
    60 minutes apart by the lease brake."""
    monkeypatch.setattr(ref_lease, "ATTEMPT_COOLDOWN_MIN", 0)
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


def test_claim_failure_aborts_cleanly(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim-level failure processes nothing: the pass returns
    ``failed: 1`` and stamps no error tags — there is no batch
    transaction to roll back (per-paper commits since the OpenAlex
    leg: one paper's commit failure can only strand that paper)."""
    paper = seed_paper_ref(store, slug="cr-cf")
    _seed_entry(store, paper, 1, seed_paper_ref(store, slug="cr-cf-cited"))
    _seed_chunk(store, paper, 0, "prior [1].")
    run_bib_mark_pass(store, batch_size=50)
    _mark_pending(store, paper)

    def _boom(conn: Any, *, limit: int) -> list[int]:
        raise RuntimeError("claim failed")

    monkeypatch.setattr(cite_resolve, "_claim_pending_papers", _boom)

    result = run_cite_resolve_pass(store, batch_size=10)
    assert result == {
        "papers_swept": 0,
        "chunks_swept": 0,
        "links_ensured": 0,
        "entries_resolved": 0,
        "entries_missed": 0,
        "deferred": 0,
        "failed": 1,
    }
    assert store.has_tag(paper, "CITERES", "pending") is True
    assert store.has_tag(paper, "CITERES", "swept") is False

    monkeypatch.undo()
    retry = run_cite_resolve_pass(store, batch_size=10)
    assert retry["papers_swept"] == 1
    assert retry["failed"] == 0


def test_lease_excludes_and_success_clears(store: Any) -> None:
    """The attempt lease is the durable claim: a pending paper under an
    unexpired ``CITERESATTEMPT`` lease is never claimed; a swept paper
    carries no lease (``clear_attempt`` rides the success write)."""
    cited = seed_paper_ref(store, slug="cr-lease-cited")
    leased = seed_paper_ref(store, slug="cr-lease-leased")
    free = seed_paper_ref(store, slug="cr-lease-free")
    for paper in (leased, free):
        _seed_entry(store, paper, 1, cited)
        _seed_chunk(store, paper, 0, "prior [1].")
    run_bib_mark_pass(store, batch_size=50)
    for paper in (leased, free):
        _mark_pending(store, paper)
    ref_lease.stamp_attempt(store, leased, "CITERES", conn=None)

    result = run_cite_resolve_pass(store, batch_size=10)
    assert result["papers_swept"] == 1
    assert store.has_tag(free, "CITERES", "swept") is True
    assert store.has_tag(leased, "CITERES", "pending") is True
    # the swept paper's lease was cleared in the success transaction
    assert store.has_tag(free, "CITERESATTEMPT", "1") is False
    # the braked paper's own lease is still up
    assert store.has_tag(leased, "CITERESATTEMPT", "1") is True


# ── the OpenAlex leg (slice-4 acceptance) ────────────────────────────


def _add_identifier(store: Any, ref_id: int, id_kind: str, id_value: str) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES (%s, %s, %s, %s)",
            (id_kind, id_value, ref_id, "test"),
        )
        conn.commit()


def _entry_row(store: Any, entry_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT held_ref_id, openalex_id, doi, match_conf "
            "FROM paper_bib_entries WHERE id = %s",
            (entry_id,),
        ).fetchone()
    assert row is not None
    return {
        "held_ref_id": row[0],
        "openalex_id": row[1],
        "doi": row[2],
        "match_conf": row[3],
    }


def _ref_level_cites(store: Any, ref_id: int) -> list[Any]:
    return [l for l in _cites_links(store, ref_id) if l.src_chunk_id is None]


def _oa_work(
    wid: str, title: str, year: int, first_author: str, doi: str | None = None
) -> dict[str, Any]:
    return {
        "id": f"https://openalex.org/{wid}",
        "title": title,
        "display_name": title,
        "publication_year": year,
        "doi": f"https://doi.org/{doi}" if doi else None,
        "authorships": [{"author": {"display_name": first_author}}],
    }


def _pending_swept_paper(store: Any, slug: str, **entry_kw: Any) -> int:
    """A citing paper with one bib entry that bib_mark has swept,
    tagged CITERES:pending — the entry is seeded before the mark pass
    (bib_mark only stamps chunks of a paper that has entries, and the
    cite_resolve claim needs that stamp)."""
    paper = seed_paper_ref(store, slug=slug)
    _seed_entry(store, paper, 1, **entry_kw)
    _seed_chunk(store, paper, 0, "prior work [1].")
    run_bib_mark_pass(store, batch_size=50)
    _mark_pending(store, paper)
    return paper


def test_oa_cached_refs_match_needs_no_lookup(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 1 of the hop: the citing paper's cached ``referenced_works``
    resolve to held refs and the entry matches on title/year — zero
    live calls, the link lands and the entry carries the w-id."""
    cited = seed_paper_ref(store, slug="cr-oa-cited")
    store.update_paper_fields(
        cited, title="The Seminal Result", year=1921, source="test"
    )
    _add_identifier(store, cited, "openalex", "w100")
    paper = seed_paper_ref(store, slug="cr-oa-citing")
    store.update_paper_fields(
        paper,
        meta_patch={"openalex": {"referenced_works": ["W100"]}},
        source="test",
    )
    entry = _seed_entry(
        store,
        paper,
        1,
        authors="Miller, A.",
        year=1921,
        raw_text="Miller, A. The Seminal Result. J. Phys. 1921, 1, 2.",
        match_conf=0.0,
    )
    _seed_chunk(store, paper, 0, "as shown before [1].")
    run_bib_mark_pass(store, batch_size=50)
    _mark_pending(store, paper)

    calls = {"n": 0}

    def _counting(*a: Any, **k: Any) -> list[Any]:
        calls["n"] += 1
        return []

    monkeypatch.setattr(cite_resolve.openalex_meta, "search_openalex_works", _counting)
    result = run_cite_resolve_pass(store, batch_size=10)

    assert calls["n"] == 0  # resolved locally, no live call
    assert result["entries_resolved"] == 1
    row = _entry_row(store, entry)
    assert row["held_ref_id"] == cited
    assert row["openalex_id"] == "W100"
    # chunk-scope link from the sweep + paper-level edge from the leg
    assert len(_cites_links(store, paper)) == 2
    assert len(_ref_level_cites(store, paper)) == 1


def test_oa_live_lookup_held_resolves(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 2: no cached refs — the live ``/works?search=`` hit is
    verified (title in raw_text, year, first-author surname), resolved
    to held by its w-id, and the chunk links."""
    cited = seed_paper_ref(store, slug="cr-oa-live-cited")
    _add_identifier(store, cited, "openalex", "w200")
    paper = seed_paper_ref(store, slug="cr-oa-live-citing")
    entry = _seed_entry(
        store,
        paper,
        1,
        authors="Miller, A.",
        year=1921,
        raw_text="Miller, A. Surface Kinetics. J. Phys. 1921, 1, 2.",
        match_conf=0.0,
    )
    _seed_chunk(store, paper, 0, "as shown before [1].")
    run_bib_mark_pass(store, batch_size=50)
    _mark_pending(store, paper)

    monkeypatch.setattr(
        cite_resolve.openalex_meta,
        "search_openalex_works",
        lambda *a, **k: [_oa_work("W200", "Surface Kinetics", 1921, "A. Miller")],
    )
    result = run_cite_resolve_pass(store, batch_size=10)

    assert result["entries_resolved"] == 1
    row = _entry_row(store, entry)
    assert row["held_ref_id"] == cited
    assert row["openalex_id"] == "W200"
    assert len(_cites_links(store, paper)) == 2


def test_oa_live_lookup_unheld_is_the_stub_request(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verified hit on an unheld work writes ``openalex_id`` (+ its
    DOI) on the entry — the stub request the gr513755 trickle mints
    under its own budget. No stub, no link, from this path."""
    paper = seed_paper_ref(store, slug="cr-oa-unheld")
    entry = _seed_entry(
        store,
        paper,
        1,
        authors="Miller, A.",
        year=1921,
        raw_text="Miller, A. Surface Kinetics. J. Phys. 1921, 1, 2.",
        match_conf=0.0,
    )
    _seed_chunk(store, paper, 0, "as shown before [1].")
    run_bib_mark_pass(store, batch_size=50)
    _mark_pending(store, paper)

    calls = {"n": 0}

    def _fake(*a: Any, **k: Any) -> list[Any]:
        calls["n"] += 1
        return [_oa_work("W300", "Surface Kinetics", 1921, "A. Miller", "10.9/x")]

    monkeypatch.setattr(cite_resolve.openalex_meta, "search_openalex_works", _fake)
    result = run_cite_resolve_pass(store, batch_size=10)

    assert calls["n"] == 1
    assert result["entries_resolved"] == 1
    row = _entry_row(store, entry)
    assert row["openalex_id"] == "W300"
    assert row["doi"] == "10.9/x"
    assert row["held_ref_id"] is None
    assert _cites_links(store, paper) == []


def test_oa_miss_stamps_decided_entry_only(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stamped miss: a decided row (``match_conf`` non-NULL) gets
    ``-2.0``; a crossref-undecided row (NULL) stays NULL so
    ``run_bib_rematch_sweep`` keeps its retry."""
    paper = seed_paper_ref(store, slug="cr-oa-miss")
    decided = _seed_entry(
        store, paper, 1, raw_text="Miller, A. Untitled. J. 1921.", match_conf=0.0
    )
    undecided = _seed_entry(
        store, paper, 2, raw_text="Smith, B. Other. J. 1922.", match_conf=None
    )
    _seed_chunk(store, paper, 0, "cf. [1] and [2].")
    run_bib_mark_pass(store, batch_size=50)
    _mark_pending(store, paper)

    calls = {"n": 0}

    def _none(*a: Any, **k: Any) -> list[Any]:
        calls["n"] += 1
        return []

    monkeypatch.setattr(cite_resolve.openalex_meta, "search_openalex_works", _none)
    result = run_cite_resolve_pass(store, batch_size=10)

    assert calls["n"] == 2
    assert result["entries_missed"] == 2
    assert _entry_row(store, decided)["match_conf"] == -2.0
    assert _entry_row(store, undecided)["match_conf"] is None


def test_oa_ambiguous_hit_writes_nothing(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two candidates through the verify gate = ambiguous → the miss
    stamp, no link (wrong is the only unacceptable outcome)."""
    paper = seed_paper_ref(store, slug="cr-oa-ambig")
    entry = _seed_entry(
        store,
        paper,
        1,
        authors="Miller, A.",
        year=1921,
        raw_text="Miller, A. Surface Kinetics. J. Phys. 1921, 1, 2.",
        match_conf=0.0,
    )
    _seed_chunk(store, paper, 0, "as shown before [1].")
    run_bib_mark_pass(store, batch_size=50)
    _mark_pending(store, paper)

    monkeypatch.setattr(
        cite_resolve.openalex_meta,
        "search_openalex_works",
        lambda *a, **k: [
            _oa_work("W301", "Surface Kinetics", 1921, "A. Miller"),
            _oa_work("W302", "Surface Kinetics", 1921, "A. Miller"),
        ],
    )
    result = run_cite_resolve_pass(store, batch_size=10)

    assert result["entries_missed"] == 1
    assert result["entries_resolved"] == 0
    row = _entry_row(store, entry)
    assert row["match_conf"] == -2.0
    assert row["openalex_id"] is None
    assert _cites_links(store, paper) == []


def test_oa_doi_recheck_needs_no_lookup(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An identifier-carrying unheld entry re-checks locally only: a
    ``doi`` entry whose target became held since matching resolves
    without any OpenAlex call."""
    cited = seed_paper_ref(store, slug="cr-oa-doi-cited")
    _add_identifier(store, cited, "doi", "10.9/recheck")
    paper = seed_paper_ref(store, slug="cr-oa-doi-citing")
    entry = _seed_entry(store, paper, 1, doi="10.9/recheck", match_conf=0.8)
    _seed_chunk(store, paper, 0, "prior [1].")
    run_bib_mark_pass(store, batch_size=50)
    _mark_pending(store, paper)

    calls = {"n": 0}

    def _counting(*a: Any, **k: Any) -> list[Any]:
        calls["n"] += 1
        return []

    monkeypatch.setattr(cite_resolve.openalex_meta, "search_openalex_works", _counting)
    result = run_cite_resolve_pass(store, batch_size=10)

    assert calls["n"] == 0
    assert result["entries_resolved"] == 1
    row = _entry_row(store, entry)
    assert row["held_ref_id"] == cited
    assert len(_cites_links(store, paper)) == 2


def test_oa_ratelimited_defers_never_errors(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Limiter refusal defers: the refused paper keeps ``pending`` and
    its lease (it was attempted — the brake), takes no error stamp;
    papers never attempted get their lease back and re-claim next
    pass."""
    first = _pending_swept_paper(
        store,
        "cr-oa-rl-first",
        raw_text="Miller, A. Surface Kinetics. J. Phys. 1921.",
        match_conf=0.0,
    )
    second = _pending_swept_paper(
        store,
        "cr-oa-rl-second",
        raw_text="Miller, A. Surface Kinetics. J. Phys. 1921.",
        match_conf=0.0,
    )
    assert first < second  # claim order — the first paper is refused

    def _refused(*a: Any, **k: Any) -> list[Any]:
        raise RateLimited("openalex")

    monkeypatch.setattr(cite_resolve.openalex_meta, "search_openalex_works", _refused)
    result = run_cite_resolve_pass(store, batch_size=10)

    assert result["deferred"] == 1
    assert result["failed"] == 0
    assert result["papers_swept"] == 0
    assert store.has_tag(first, "CITERES", "pending") is True
    assert store.has_tag(first, "CITERES", "error:1") is False
    # the attempted paper keeps its brake; the untouched one is free
    assert store.has_tag(first, "CITERESATTEMPT", "1") is True
    assert store.has_tag(second, "CITERESATTEMPT", "1") is False

    # Next pass with the limiter back: the untouched paper claims and
    # sweeps (its live lookup misses); the braked one still waits out
    # its lease.
    monkeypatch.setattr(
        cite_resolve.openalex_meta, "search_openalex_works", lambda *a, **k: []
    )
    result = run_cite_resolve_pass(store, batch_size=10)
    assert result["papers_swept"] == 1
    assert store.has_tag(second, "CITERES", "swept") is True
    assert store.has_tag(first, "CITERES", "pending") is True

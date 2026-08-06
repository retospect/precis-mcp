"""Tests for the bib_parse pass (``paper_bib_entries``,
docs/proposals/citation-bib-parse.md).

Pure helpers (chunk detection, entry splitting/dedup, field extraction,
candidate resolution) run everywhere, no DB. The end-to-end pass runs
against real PG (the ``store`` fixture) with a fake LLM client; the
matcher tests additionally monkeypatch
``precis.utils.safe_fetch.safe_get`` — no network anywhere in this file.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import precis.workers.bib_parse as bib_parse_mod
from precis.workers.bib_parse import (
    BIB_PARSE_VERSION,
    _chunk_is_bibliography,
    _collect_paper_entries,
    _extract_acs_fields,
    _extract_doi_from_text,
    _parse_paper_entries,
    _resolve_crossref_candidates,
    _split_entries,
    run_bib_parse_match_pass,
    run_bib_parse_pass,
)
from tests.workers._helpers import seed_chunk, seed_ref

#: The proposal's worked example (AC 1, pa42553 entry [126]) minus the
#: leading ``- [126] `` marker (raw_text excludes it -- see
#: ``_split_entries``).
ACS_LINE = (
    "Z. Ali, M. Mehmood, J. Ahmad, X. Li, A. Majeed, H. Tabassum, P. X. Hou, "
    "C. Liu, ChemCatChem 2020, 12, 360."
)


class _FakeClient:
    """Records every prompt; replies with a fixed (or per-call) text."""

    def __init__(self, text: str | list[str]) -> None:
        self._texts = [text] if isinstance(text, str) else list(text)
        self.calls: list[Any] = []

    def complete(self, messages: list[dict[str, str]]) -> Any:
        self.calls.append(messages)
        idx = min(len(self.calls) - 1, len(self._texts) - 1)
        return SimpleNamespace(text=self._texts[idx], total_tokens=7)


class _FakeResponse:
    """Stands in for ``httpx.Response`` — only what ``_crossref_query`` reads."""

    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload


def _add_id(store: Any, ref_id: int, id_kind: str, id_value: str) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES (%s, %s, %s, %s)",
            (id_kind, id_value, ref_id, "test"),
        )
        conn.commit()


def _insert_entry(store: Any, ref_id: int, marker: int, raw_text: str) -> int:
    """Seed a ``paper_bib_entries`` row directly (bypassing parse) for
    matcher-only tests."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO paper_bib_entries (ref_id, marker, raw_text, parse_version) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (ref_id, marker, raw_text, BIB_PARSE_VERSION),
        ).fetchone()
        conn.commit()
    assert row is not None
    return int(row[0])


def _entry_row(store: Any, entry_id: int) -> tuple[Any, ...]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT doi, s2_id, held_ref_id, match_conf FROM paper_bib_entries "
            "WHERE id = %s",
            (entry_id,),
        ).fetchone()
    assert row is not None
    return tuple(row)


def _bib_rows(store: Any, ref_id: int) -> list[tuple[Any, ...]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT marker, authors, journal, year, volume, first_page, "
            "parse_conf, doi, match_conf, parse_version "
            "FROM paper_bib_entries WHERE ref_id = %s ORDER BY marker",
            (ref_id,),
        ).fetchall()
    return [tuple(r) for r in rows]


def _meta_version(store: Any, ref_id: int) -> int | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->>'bib_parse_version' FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


# ── content-based chunk detection (AC 1) ────────────────────────────────


class TestChunkDetection:
    def test_paragraph_kind_bib_chunk_accepted(self) -> None:
        text = "\n".join(
            f"- [{i}] Some Author, Journal, 201{i % 9}, {i}, {i * 10}."
            for i in range(1, 6)
        )
        assert _chunk_is_bibliography(text, "paragraph") is True

    def test_prose_chunk_with_incidental_marker_rejected(self) -> None:
        text = (
            "As shown previously [3], the catalyst outperforms the baseline "
            "under every tested condition, and the trend continues in later "
            "work as well, confirming the mechanism proposed earlier on."
        )
        assert _chunk_is_bibliography(text, "paragraph") is False

    def test_references_chunk_kind_always_qualifies(self) -> None:
        # Even with no marker-shaped lines at all -- chunk_kind is authoritative.
        assert _chunk_is_bibliography("not much here", "references") is True

    def test_empty_chunk_is_not_bibliography(self) -> None:
        assert _chunk_is_bibliography("   \n   \n", "paragraph") is False

    def test_exact_half_ratio_qualifies(self) -> None:
        # One marker line + one wrapped continuation line = 1/2 = 0.5 exactly
        # -- must still qualify (>=, not >).
        text = "- [1] First part of a long\nentry that wraps onto a second line."
        assert _chunk_is_bibliography(text, "paragraph") is True


# ── entry splitting + overlap dedup (AC 1) ──────────────────────────────


class TestSplitEntries:
    def test_single_line_entries(self) -> None:
        text = "- [1] First entry.\n- [2] Second entry."
        assert _split_entries(text) == [(1, "First entry."), (2, "Second entry.")]

    def test_wrapped_continuation_line_is_folded_in(self) -> None:
        text = "- [1] First part of a long\nentry that wraps onto a second line."
        assert _split_entries(text) == [
            (1, "First part of a long entry that wraps onto a second line.")
        ]

    def test_text_before_first_marker_is_dropped(self) -> None:
        text = "References\n- [1] Entry one."
        assert _split_entries(text) == [(1, "Entry one.")]

    def test_oversized_marker_does_not_overflow_int(self) -> None:
        # An OCR-garbled marker with more than 4 digits must never reach
        # `int()`/INSERT as a "new" marker -- it folds into the preceding
        # entry as ordinary continuation text instead (gr: int4 overflow
        # would otherwise fail this paper's parse every cycle, forever).
        text = "- [1] Entry one.\n- [123456789012] Garbled OCR marker line."
        assert _split_entries(text) == [
            (1, "Entry one. - [123456789012] Garbled OCR marker line.")
        ]

    def test_four_digit_marker_still_matches(self) -> None:
        assert _split_entries("- [9999] Entry.") == [(9999, "Entry.")]


class TestCollectPaperEntries:
    def test_overlap_duplicate_marker_first_occurrence_wins(self) -> None:
        chunk1 = "- [1] Entry one.\n- [2] Entry two ORIGINAL."
        chunk2 = "- [2] Entry two DUPLICATE.\n- [3] Entry three."
        rows = [(0, chunk1, "paragraph"), (1, chunk2, "paragraph")]
        assert _collect_paper_entries(rows) == [
            (1, "Entry one."),
            (2, "Entry two ORIGINAL."),
            (3, "Entry three."),
        ]

    def test_non_bibliography_chunk_contributes_nothing(self) -> None:
        prose = (
            "Just some prose mentioning [3] once here, with nothing else "
            "in this chunk resembling a bibliography line at all."
        )
        bib = "- [1] Entry one.\n- [2] Entry two."
        rows = [(0, prose, "paragraph"), (1, bib, "paragraph")]
        assert _collect_paper_entries(rows) == [(1, "Entry one."), (2, "Entry two.")]


# ── ACS field extraction + DOI extraction (AC 1) ────────────────────────


class TestAcsFieldExtraction:
    def test_worked_example_matches_ac1(self) -> None:
        fields = _extract_acs_fields(ACS_LINE)
        assert fields is not None
        assert fields["journal"] == "ChemCatChem"
        assert fields["year"] == 2020
        assert fields["volume"] == "12"
        assert fields["first_page"] == "360"
        assert fields["authors"].startswith("Z. Ali")
        assert fields["authors"].endswith("C. Liu")

    def test_messy_line_does_not_match(self) -> None:
        assert _extract_acs_fields("See the supplementary information.") is None


class TestDoiExtraction:
    def test_extracts_embedded_doi(self) -> None:
        text = "... available at https://doi.org/10.1002/cctc.201901234."
        assert _extract_doi_from_text(text) == "10.1002/cctc.201901234"

    def test_no_doi_in_text_returns_none(self) -> None:
        assert _extract_doi_from_text(ACS_LINE) is None


# ── LLM parse fallback (AC 1 — messy line routed to SMALL, mocked) ─────


class TestLlmParseFallback:
    def test_messy_line_routed_to_llm_only(self) -> None:
        messy = "See the supplementary information for further detail."
        client = _FakeClient(
            '{"entries":[{"marker":2,"authors":null,"journal":"Foo",'
            '"year":2019,"volume":"3","first_page":"9"}]}'
        )
        rows = _parse_paper_entries(client, [(1, ACS_LINE), (2, messy)])
        assert len(client.calls) == 1  # only the messy line needed the model
        by_marker = {r["marker"]: r for r in rows}
        assert by_marker[1]["parse_conf"] == pytest.approx(0.9)  # regex path
        assert by_marker[1]["journal"] == "ChemCatChem"
        assert by_marker[2]["journal"] == "Foo"
        assert by_marker[2]["year"] == 2019
        assert by_marker[2]["parse_conf"] == pytest.approx(0.55)

    def test_unparseable_llm_reply_marks_unparsed(self) -> None:
        client = _FakeClient("sorry, I cannot help with that")
        rows = _parse_paper_entries(client, [(1, "totally garbled, no shape at all")])
        assert rows[0]["parse_conf"] == 0.0
        assert rows[0]["journal"] is None
        assert rows[0]["authors"] is None


# ── Crossref candidate resolution (pure) ────────────────────────────────


class TestResolveCrossrefCandidates:
    def test_no_candidates_is_unmatched(self) -> None:
        doi, conf = _resolve_crossref_candidates(_FakeClient("{}"), "x", [])
        assert doi is None
        assert conf == 0.0

    def test_single_confident_candidate_matches(self) -> None:
        items = [{"DOI": "10.1/a", "score": 80.0}]
        doi, conf = _resolve_crossref_candidates(_FakeClient("{}"), "x", items)
        assert doi == "10.1/a"
        assert conf == pytest.approx(0.8)

    def test_clear_score_gap_matches_top_without_adjudication(self) -> None:
        items = [{"DOI": "10.1/a", "score": 90.0}, {"DOI": "10.1/b", "score": 10.0}]
        client = _FakeClient("{}")
        doi, conf = _resolve_crossref_candidates(client, "x", items)
        assert doi == "10.1/a"
        assert conf == pytest.approx(0.8)
        assert client.calls == []  # no adjudication needed

    def test_close_scores_are_adjudicated(self) -> None:
        items = [{"DOI": "10.1/a", "score": 90.0}, {"DOI": "10.1/b", "score": 89.0}]
        client = _FakeClient('{"pick": "b"}')
        doi, conf = _resolve_crossref_candidates(client, "x", items)
        assert doi == "10.1/b"
        assert conf == pytest.approx(0.6)
        assert len(client.calls) == 1


# ── end-to-end pass (real PG, mocked Crossref via _crossref_query) ─────


class TestPassParse:
    def test_acs_worked_example_and_overlap_dedup(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Match step isn't under test here -- keep it network-free + inert.
        monkeypatch.setattr(bib_parse_mod, "_crossref_query", lambda *a, **k: [])
        chunk1 = "\n".join(
            [
                "- [124] A. One, B. Two, Nature 2018, 5, 100.",
                "- [125] C. Three, D. Four, Science 2019, 8, 200.",
                "- [126] " + ACS_LINE,
            ]
        )
        chunk2 = "\n".join(
            [
                "- [125] C. Three, D. Four, Science 2019, 8, 200.",  # overlap dup
                "- [126] " + ACS_LINE,  # overlap dup
                "- [127] E. Five, F. Six, Chem. Rev. 2021, 121, 50.",
            ]
        )
        ref_id = seed_ref(store, title="A carbon-catalysis paper")
        seed_chunk(store, ref_id=ref_id, ord=0, chunk_kind="paragraph", text=chunk1)
        seed_chunk(store, ref_id=ref_id, ord=1, chunk_kind="paragraph", text=chunk2)
        client = _FakeClient("{}")

        result = run_bib_parse_pass(
            store, client=client, batch_size=10, ref_ids=[ref_id]
        )
        assert result == {"claimed": 1, "ok": 1, "failed": 0}
        assert client.calls == []  # every line matched the ACS regex

        rows = _bib_rows(store, ref_id)
        markers = [r[0] for r in rows]
        assert markers == [124, 125, 126, 127]  # distinct, deduped, ordered

        row126 = next(r for r in rows if r[0] == 126)
        (
            _marker,
            _authors,
            journal,
            year,
            volume,
            first_page,
            parse_conf,
            doi,
            match_conf,
            parse_version,
        ) = row126
        assert journal == "ChemCatChem"
        assert year == 2020
        assert volume == "12"
        assert first_page == "360"
        assert parse_conf == pytest.approx(0.9)
        assert parse_version == BIB_PARSE_VERSION
        assert doi is None  # mocked crossref found nothing
        assert match_conf == pytest.approx(0.0)  # attempted, memoized unmatched

        assert _meta_version(store, ref_id) == BIB_PARSE_VERSION


class TestConvergence:
    def test_no_bibliography_paper_stamped_and_not_reclaimed(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bib_parse_mod, "_crossref_query", lambda *a, **k: [])
        ref_id = seed_ref(store, title="an ordinary paper")
        seed_chunk(
            store,
            ref_id=ref_id,
            ord=0,
            chunk_kind="paragraph",
            text="Just an ordinary paragraph with no citations in it at all.",
        )
        client = _FakeClient("{}")

        first = run_bib_parse_pass(
            store, client=client, batch_size=10, ref_ids=[ref_id]
        )
        assert first == {"claimed": 1, "ok": 1, "failed": 0}
        assert _meta_version(store, ref_id) == BIB_PARSE_VERSION
        assert _bib_rows(store, ref_id) == []

        second = run_bib_parse_pass(
            store, client=client, batch_size=10, ref_ids=[ref_id]
        )
        assert second == {"claimed": 0, "ok": 0, "failed": 0}

    def test_version_bump_resweeps(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bib_parse_mod, "_crossref_query", lambda *a, **k: [])
        ref_id = seed_ref(store, title="an ordinary paper")
        seed_chunk(
            store, ref_id=ref_id, ord=0, chunk_kind="paragraph", text="plain prose."
        )
        client = _FakeClient("{}")

        first = run_bib_parse_pass(
            store, client=client, batch_size=10, ref_ids=[ref_id]
        )
        assert first["claimed"] == 1

        still = run_bib_parse_pass(
            store, client=client, batch_size=10, ref_ids=[ref_id]
        )
        assert still["claimed"] == 0

        monkeypatch.setattr(bib_parse_mod, "BIB_PARSE_VERSION", BIB_PARSE_VERSION + 1)
        bumped = run_bib_parse_pass(
            store, client=client, batch_size=10, ref_ids=[ref_id]
        )
        assert bumped["claimed"] == 1
        assert _meta_version(store, ref_id) == BIB_PARSE_VERSION + 1

    def test_version_bump_rewrites_stale_rows_not_just_skips(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # gripe: a bare `ON CONFLICT (ref_id, marker) DO NOTHING` re-scan
        # would leave existing rows (and their stale match_conf) untouched
        # forever -- prove the DELETE-before-INSERT actually rewrites them.
        monkeypatch.setattr(bib_parse_mod, "_crossref_query", lambda *a, **k: [])
        ref_id = seed_ref(store, title="a bibliography paper")
        seed_chunk(
            store,
            ref_id=ref_id,
            ord=0,
            chunk_kind="paragraph",
            text="- [1] A. One, B. Two, Nature 2018, 5, 100.",
        )
        client = _FakeClient("{}")

        first = run_bib_parse_pass(
            store, client=client, batch_size=10, ref_ids=[ref_id]
        )
        assert first == {"claimed": 1, "ok": 1, "failed": 0}
        rows = _bib_rows(store, ref_id)
        assert len(rows) == 1
        marker, _authors, journal, *_rest, match_conf, parse_version = rows[0]
        assert marker == 1
        assert journal == "Nature"
        assert parse_version == BIB_PARSE_VERSION
        assert match_conf == pytest.approx(0.0)  # attempted, memoized unmatched

        monkeypatch.setattr(bib_parse_mod, "BIB_PARSE_VERSION", BIB_PARSE_VERSION + 1)
        # Freeze the match step so the DELETE+INSERT rewrite is observable
        # in isolation, before anything re-matches it -- the matcher's own
        # idempotency is already covered by TestMatchCrossref.
        monkeypatch.setattr(
            bib_parse_mod, "run_bib_parse_match_pass", lambda *a, **k: {"attempted": 0}
        )

        bumped = run_bib_parse_pass(
            store, client=client, batch_size=10, ref_ids=[ref_id]
        )
        assert bumped == {"claimed": 1, "ok": 1, "failed": 0}
        rows_after = _bib_rows(store, ref_id)
        assert len(rows_after) == 1  # rewritten in place, not duplicated
        marker, _authors, journal, *_rest, match_conf, parse_version = rows_after[0]
        assert marker == 1
        assert journal == "Nature"
        assert parse_version == BIB_PARSE_VERSION + 1
        assert match_conf is None  # reset -- the stale row was deleted + reinserted


# ── matcher (AC 3 — mocked Crossref, memoization, safe_get-only) ───────


class TestMatchLocalDoi:
    def test_local_doi_exact_against_s2_neighbors_skips_crossref(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref_id = seed_ref(store, title="citing paper")
        held_id = seed_ref(store, title="the cited paper")
        _add_id(store, held_id, "doi", "10.1002/cctc.201901234")
        store.replace_s2_neighbors(
            ref_id,
            "cites",
            [
                {
                    "s2_id": "S2X",
                    "doi": "10.1002/cctc.201901234",
                    "title": "T",
                    "year": 2020,
                    "held_ref_id": None,
                }
            ],
        )
        raw_text = (
            "Some Author, ChemCatChem 2020, 12, 360, "
            "https://doi.org/10.1002/cctc.201901234."
        )
        entry_id = _insert_entry(store, ref_id, 1, raw_text)

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("a local DOI hit must not query Crossref")

        monkeypatch.setattr("precis.utils.safe_fetch.safe_get", _boom)

        result = run_bib_parse_match_pass(store, ref_id, client=_FakeClient("{}"))
        assert result == {"attempted": 1}
        doi, s2_id, held_ref_id, match_conf = _entry_row(store, entry_id)
        assert doi == "10.1002/cctc.201901234"
        assert s2_id == "S2X"
        assert held_ref_id == held_id
        assert match_conf == pytest.approx(1.0)

    def test_text_doi_not_in_s2_neighbors_falls_through_to_crossref(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Decided policy: step 1 is DOI-exact against s2_neighbors ONLY --
        # a raw-text DOI that S2 never saw still falls through to Crossref.
        ref_id = seed_ref(store, title="citing paper")
        raw_text = "Some Author, Journal, 2020, https://doi.org/10.9/unseen."
        entry_id = _insert_entry(store, ref_id, 1, raw_text)

        calls: list[Any] = []

        def fake_safe_get(client: Any, url: str, /, **kw: Any) -> Any:
            calls.append(url)
            return _FakeResponse(200, {"message": {"items": []}})

        monkeypatch.setattr("precis.utils.safe_fetch.safe_get", fake_safe_get)

        run_bib_parse_match_pass(store, ref_id, client=_FakeClient("{}"))
        assert len(calls) == 1
        doi, _s2_id, _held, match_conf = _entry_row(store, entry_id)
        assert doi is None
        assert match_conf == pytest.approx(0.0)


class TestMatchCrossref:
    def test_confident_candidate_matches_via_safe_get(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref_id = seed_ref(store, title="citing paper")
        entry_id = _insert_entry(
            store, ref_id, 1, "A messy citation with no DOI, Some Journal, 2020."
        )
        calls: list[str] = []

        def fake_safe_get(client: Any, url: str, /, **kw: Any) -> Any:
            calls.append(url)
            return _FakeResponse(
                200,
                {
                    "message": {
                        "items": [
                            {
                                "DOI": "10.1/aaa",
                                "score": 90.0,
                                "title": ["A Paper"],
                                "container-title": ["Some Journal"],
                                "issued": {"date-parts": [[2020]]},
                            }
                        ]
                    }
                },
            )

        monkeypatch.setattr("precis.utils.safe_fetch.safe_get", fake_safe_get)
        # No raw-httpx bypass: fail loud if the code ever calls client.get
        # directly instead of going through safe_get (SSRF guard).
        import httpx

        def _boom_get(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("crossref fetch must go through safe_get")

        monkeypatch.setattr(httpx.Client, "get", _boom_get)

        result = run_bib_parse_match_pass(store, ref_id, client=_FakeClient("{}"))
        assert result == {"attempted": 1}
        assert calls == [bib_parse_mod._CROSSREF_BASE]
        doi, _s2_id, _held, match_conf = _entry_row(store, entry_id)
        assert doi == "10.1/aaa"
        assert match_conf == pytest.approx(0.8)

    def test_ambiguous_candidates_adjudicated_by_llm(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref_id = seed_ref(store, title="citing paper")
        entry_id = _insert_entry(
            store, ref_id, 1, "An ambiguous citation, Some Journal, 2020."
        )

        def fake_safe_get(client: Any, url: str, /, **kw: Any) -> Any:
            return _FakeResponse(
                200,
                {
                    "message": {
                        "items": [
                            {"DOI": "10.1/aaa", "score": 90.0},
                            {"DOI": "10.1/bbb", "score": 89.0},
                        ]
                    }
                },
            )

        monkeypatch.setattr("precis.utils.safe_fetch.safe_get", fake_safe_get)
        client = _FakeClient('{"pick": "b"}')

        run_bib_parse_match_pass(store, ref_id, client=client)
        assert len(client.calls) == 1  # the adjudication call
        doi, _s2_id, _held, match_conf = _entry_row(store, entry_id)
        assert doi == "10.1/bbb"
        assert match_conf == pytest.approx(0.6)

    def test_no_candidates_memoized_second_pass_zero_crossref_calls(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref_id = seed_ref(store, title="citing paper")
        entry_id = _insert_entry(
            store, ref_id, 1, "A totally unfindable citation, nowhere, 1899."
        )
        call_count = {"n": 0}

        def fake_safe_get(client: Any, url: str, /, **kw: Any) -> Any:
            call_count["n"] += 1
            return _FakeResponse(200, {"message": {"items": []}})

        monkeypatch.setattr("precis.utils.safe_fetch.safe_get", fake_safe_get)

        first = run_bib_parse_match_pass(store, ref_id, client=_FakeClient("{}"))
        assert first == {"attempted": 1}
        assert call_count["n"] == 1
        doi, _s2_id, _held, match_conf = _entry_row(store, entry_id)
        assert doi is None
        assert match_conf == pytest.approx(0.0)

        # Second pass: match_conf is already non-NULL -- nothing to attempt,
        # so no Crossref call is even considered (spy-asserted).
        second = run_bib_parse_match_pass(store, ref_id, client=_FakeClient("{}"))
        assert second == {"attempted": 0}
        assert call_count["n"] == 1

    def test_transient_error_retries_then_succeeds(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref_id = seed_ref(store, title="citing paper")
        entry_id = _insert_entry(
            store, ref_id, 1, "A rate-limited citation, Some Journal, 2020."
        )
        responses = [
            _FakeResponse(429),
            _FakeResponse(
                200, {"message": {"items": [{"DOI": "10.1/retry", "score": 50.0}]}}
            ),
        ]

        def fake_safe_get(client: Any, url: str, /, **kw: Any) -> Any:
            return responses.pop(0)

        monkeypatch.setattr("precis.utils.safe_fetch.safe_get", fake_safe_get)
        monkeypatch.setattr(bib_parse_mod.time, "sleep", lambda *_a: None)

        run_bib_parse_match_pass(store, ref_id, client=_FakeClient("{}"))
        doi, _s2_id, _held, _match_conf = _entry_row(store, entry_id)
        assert doi == "10.1/retry"
        assert responses == []  # both canned responses consumed

    def test_persistent_failure_leaves_match_conf_null_for_retry(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A Crossref outage (every attempt fails) must NOT be memoized as
        # "unmatched" -- that would permanently poison every entry touched
        # during the outage window. match_conf must stay NULL so a later
        # attempt retries it.
        ref_id = seed_ref(store, title="citing paper")
        entry_id = _insert_entry(
            store, ref_id, 1, "A network-flaky citation, Some Journal, 2020."
        )

        def fake_safe_get(client: Any, url: str, /, **kw: Any) -> Any:
            raise RuntimeError("connection reset")

        monkeypatch.setattr("precis.utils.safe_fetch.safe_get", fake_safe_get)
        monkeypatch.setattr(bib_parse_mod.time, "sleep", lambda *_a: None)

        result = run_bib_parse_match_pass(store, ref_id, client=_FakeClient("{}"))
        assert result == {"attempted": 1}
        doi, _s2_id, _held, match_conf = _entry_row(store, entry_id)
        assert doi is None
        assert match_conf is None  # NOT memoized -- eligible for retry

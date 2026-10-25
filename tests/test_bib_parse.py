"""Tests for the bib_parse pass (``paper_bib_entries``,
``citation-bib-parse`` (git-only)).

Pure helpers (chunk detection, entry splitting/dedup, field extraction,
candidate resolution) run everywhere, no DB. The end-to-end pass runs
against real PG (the ``store`` fixture) with a fake LLM client; the
matcher tests additionally monkeypatch
``precis.utils.safe_fetch.safe_get`` — no network anywhere in this file.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import precis.workers.bib_parse as bib_parse_mod
from precis.utils import http
from precis.workers import ref_lease
from precis.workers.bib_parse import (
    BIB_PARSE_VERSION,
    _chunk_is_bibliography,
    _claim,
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

_NEEDS_FCNTL = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: precis.utils.stall_capture (crossref limiter) imports fcntl",
)


@pytest.fixture(autouse=True)
def _shared_limiter_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the real shared limiter; refusal tests re-patch it."""
    monkeypatch.setattr("precis.utils.rate_limit.require", lambda p, **_: None)


def _refuse(provider: str, **_: Any) -> None:
    from precis.utils.rate_limit import RateLimited

    raise RateLimited(provider)


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
        self.headers: dict[str, str] = {}
        self.text = ""

    @property
    def request(self) -> Any:
        raise RuntimeError("no request attached")

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


class TestNoConnectionHeldDuringLlmCall:
    def test_llm_parse_runs_with_no_db_connection_checked_out(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # gr: the LLM parse call must not run inside an open transaction --
        # holding a connection idle across it trips Postgres's
        # idle_in_transaction_session_timeout and fails the paper (see
        # ``citation-bib-parse`` (git-only)). Prove it by having the
        # fake client's `complete`, while it runs, successfully acquire and
        # use a *fresh* connection from the same pool -- if the pass were
        # still holding one open (e.g. pool exhaustion, or blocked on
        # itself for a single-connection pool), this would hang or fail.
        monkeypatch.setattr(bib_parse_mod, "_crossref_query", lambda *a, **k: [])

        class _ConnCheckingClient:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, messages: list[dict[str, str]]) -> Any:
                self.calls += 1
                with store.pool.connection() as conn:
                    row = conn.execute("SELECT 1").fetchone()
                assert row == (1,)
                return SimpleNamespace(
                    text='{"entries":[{"marker":1,"authors":null,'
                    '"journal":"Foo","year":2019,"volume":"3",'
                    '"first_page":"9"}]}',
                    total_tokens=7,
                )

        ref_id = seed_ref(store, title="a messy paper")
        seed_chunk(
            store,
            ref_id=ref_id,
            ord=0,
            chunk_kind="paragraph",
            text="- [1] See the supplementary information for further detail.",
        )
        client = _ConnCheckingClient()

        result = run_bib_parse_pass(
            store, client=client, batch_size=10, ref_ids=[ref_id]
        )
        assert result == {"claimed": 1, "ok": 1, "failed": 0}
        assert client.calls == 1

        rows = _bib_rows(store, ref_id)
        assert len(rows) == 1
        marker, _authors, journal, year, *_rest = rows[0]
        assert marker == 1
        assert journal == "Foo"
        assert year == 2019


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


# ── claim-time attempt lease (gripe 202116 -- cross-host double-claim) ──


class TestClaimLease:
    def test_second_claimer_skips_a_lease_held_row(self, store: Any) -> None:
        """Mirrors ``run_bib_parse_pass``'s claim step: SELECT ... FOR
        UPDATE, stamp the attempt lease, commit -- all in one transaction.
        A sibling host's ``_claim`` (a fresh transaction, after the first
        one committed and released its row lock) must not re-select the
        same paper while the lease is still live, even though nothing
        holds the row lock anymore."""
        ref_id = seed_ref(store, title="a bibliography paper")
        seed_chunk(
            store,
            ref_id=ref_id,
            ord=0,
            chunk_kind="paragraph",
            text="- [1] A. One, B. Two, Nature 2018, 5, 100.",
        )

        # Host A's claim transaction.
        with store.pool.connection() as conn:
            first_claim = _claim(conn, limit=10, ref_ids=[ref_id])
            assert first_claim == [(ref_id, "a bibliography paper")]
            for rid, _title in first_claim:
                ref_lease.stamp_attempt(store, rid, bib_parse_mod._LEASE_NS, conn=conn)
            conn.commit()

        # Host B's claim -- the row lock from host A's transaction is long
        # gone (it committed), but the durable ref_tags lease still excludes
        # this still-processing paper.
        with store.pool.connection() as conn:
            second_claim = _claim(conn, limit=10, ref_ids=[ref_id])
        assert second_claim == []

    def test_expired_lease_is_reclaimable(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crashed/wedged worker's claim must self-heal once its lease's
        TTL expires -- not strand the paper forever."""
        monkeypatch.setattr(bib_parse_mod, "_crossref_query", lambda *a, **k: [])
        ref_id = seed_ref(store, title="an ordinary paper")
        seed_chunk(
            store,
            ref_id=ref_id,
            ord=0,
            chunk_kind="paragraph",
            text="Just an ordinary paragraph with no citations in it at all.",
        )

        with store.pool.connection() as conn:
            claimed = _claim(conn, limit=10, ref_ids=[ref_id])
            assert claimed == [(ref_id, "an ordinary paper")]
            for rid, _title in claimed:
                ref_lease.stamp_attempt(store, rid, bib_parse_mod._LEASE_NS, conn=conn)
            conn.commit()

        # Still leased -- a normal sweep (real run_bib_parse_pass) does not
        # re-select this paper.
        with store.pool.connection() as conn:
            assert _claim(conn, limit=10, ref_ids=[ref_id]) == []
        result = run_bib_parse_pass(store, client=_FakeClient("{}"), ref_ids=[ref_id])
        assert result == {"claimed": 0, "ok": 0, "failed": 0}

        # Expire the lease (simulated here rather than waiting out the real
        # cooldown, mirroring axis_pass's equivalent test).
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE ref_tags SET expires_at = now() - interval '1 minute' "
                "WHERE ref_id = %s",
                (ref_id,),
            )
            conn.commit()

        retried = run_bib_parse_pass(store, client=_FakeClient("{}"), ref_ids=[ref_id])
        assert retried == {"claimed": 1, "ok": 1, "failed": 0}
        assert _meta_version(store, ref_id) == BIB_PARSE_VERSION


def test_claim_keeps_plain_ref_id_order(store: Any) -> None:
    """bib_parse's claim is NOT banded — it keeps the plain ``ref_id`` PK
    order (the early-stop the pass relies on), even when a later ref is
    quest-linked. The gr515633 backfill bands are opt-in
    (``claim_batch(banded=True)``) for the categorizer sweeps only."""
    first = seed_ref(store, title="first paper")
    seed_chunk(
        store,
        ref_id=first,
        ord=0,
        chunk_kind="paragraph",
        text="Just an ordinary paragraph with no citations in it at all.",
    )
    second = seed_ref(store, title="quest paper")
    seed_chunk(
        store,
        ref_id=second,
        ord=0,
        chunk_kind="paragraph",
        text="Another ordinary paragraph, still without a single citation.",
    )
    quest = seed_ref(store, kind="quest", title="q")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by)"
            " VALUES (%s,%s,'serves','system')",
            (second, quest),
        )
        conn.commit()

    with store.pool.connection() as conn:
        rows = _claim(conn, limit=10)
        conn.rollback()

    assert [r[0] for r in rows] == [first, second]


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

    def test_local_doi_match_against_retired_ref_leaves_held_ref_null(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # gr341498: the DOI can outlive the ref that carried it (e.g. a
        # dedup merge, or any other retirement) -- a retired ref must never
        # be handed back as the held paper.
        ref_id = seed_ref(store, title="citing paper")
        held_id = seed_ref(store, title="the cited paper")
        _add_id(store, held_id, "doi", "10.1002/cctc.201901234")
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET retired_at = now() WHERE ref_id = %s", (held_id,)
            )
            conn.commit()
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
        assert held_ref_id is None
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

    def test_414_is_terminal_and_recorded_not_retried(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr496070: a recorded 414 from Crossref marks the entry rejected
        (non-NULL ``match_conf``, no DOI) so the next pass makes no call."""
        ref_id = seed_ref(store, title="citing paper")
        entry_id = _insert_entry(store, ref_id, 1, "A citation Crossref 414s.")
        calls: list[Any] = []

        def fake_safe_get(client: Any, url: str, /, **kw: Any) -> Any:
            calls.append(kw)
            return _FakeResponse(414)

        monkeypatch.setattr("precis.utils.safe_fetch.safe_get", fake_safe_get)

        first = run_bib_parse_match_pass(store, ref_id, client=_FakeClient("{}"))
        assert first == {"attempted": 1}
        assert len(calls) == 1  # 414 is not retried within the pass either
        doi, _s2_id, _held, match_conf = _entry_row(store, entry_id)
        assert doi is None
        assert match_conf == pytest.approx(bib_parse_mod._MATCH_CONF_QUERY_REJECTED)

        second = run_bib_parse_match_pass(store, ref_id, client=_FakeClient("{}"))
        assert second == {"attempted": 0}
        assert len(calls) == 1

    def test_crossref_query_truncates_an_oversized_raw_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []

        def fake_safe_get(client: Any, url: str, /, **kw: Any) -> Any:
            seen.append(kw["params"]["query.bibliographic"])
            return _FakeResponse(200, {"message": {"items": []}})

        monkeypatch.setattr("precis.utils.safe_fetch.safe_get", fake_safe_get)
        long_text = "x" * (bib_parse_mod._CROSSREF_QUERY_MAX_CHARS * 5)
        assert bib_parse_mod._crossref_query(long_text) == []
        assert seen == ["x" * bib_parse_mod._CROSSREF_QUERY_MAX_CHARS]

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
        monkeypatch.setattr("precis.utils.http._default_sleep", lambda _s: None)

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

        calls = {"n": 0}
        sleeps: list[float] = []

        def fake_safe_get(client: Any, url: str, /, **kw: Any) -> Any:
            calls["n"] += 1
            raise httpx.ConnectError("connection reset")

        monkeypatch.setattr("precis.utils.safe_fetch.safe_get", fake_safe_get)
        monkeypatch.setattr("precis.utils.http._default_sleep", sleeps.append)
        http.reset_circuit()

        result = run_bib_parse_match_pass(store, ref_id, client=_FakeClient("{}"))
        assert result == {"attempted": 1}
        doi, _s2_id, _held, match_conf = _entry_row(store, entry_id)
        assert doi is None
        assert match_conf is None  # NOT memoized -- eligible for retry
        # The retry path really ran: 5 attempts, 4 backoff sleeps.
        assert calls["n"] == 5 and len(sleeps) == 4


# ── shared crossref limiter refusal ─────────────────────────────────────


class TestSharedLimiterRefusal:
    @_NEEDS_FCNTL
    def test_crossref_query_raises_without_calling_safe_get(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.utils.rate_limit import RateLimited

        def _no_http(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("safe_get fired despite a limiter refusal")

        monkeypatch.setattr("precis.utils.rate_limit.require", _refuse)
        monkeypatch.setattr("precis.utils.safe_fetch.safe_get", _no_http)
        with pytest.raises(RateLimited) as exc:
            bib_parse_mod._crossref_query("Some raw citation text")
        assert exc.value.provider == "crossref-list"

    def test_pass_refusal_leaves_paper_unstamped_and_stops_batch(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_http(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("safe_get fired despite a limiter refusal")

        monkeypatch.setattr("precis.utils.rate_limit.require", _refuse)
        monkeypatch.setattr("precis.utils.safe_fetch.safe_get", _no_http)
        ref_ids = []
        for i in range(2):
            ref_id = seed_ref(store, title=f"carbon paper {i}")
            seed_chunk(
                store,
                ref_id=ref_id,
                ord=0,
                chunk_kind="paragraph",
                text="- [126] " + ACS_LINE,
            )
            ref_ids.append(ref_id)
        client = _FakeClient("{}")

        result = run_bib_parse_pass(
            store, client=client, batch_size=10, ref_ids=ref_ids
        )

        assert result["claimed"] == 2
        assert result["failed"] >= 1
        assert result["ok"] == 0
        # Exactly one paper was reached (the break), and none were stamped.
        assert [_meta_version(store, r) for r in ref_ids] == [None, None]
        reached = [r for r in ref_ids if _bib_rows(store, r)]
        assert len(reached) == 1
        # The refused paper keeps its lease; the unattempted one is
        # re-claimable at once.
        with store.pool.connection() as conn:
            reclaimable = [
                r for r, _ in bib_parse_mod._claim(conn, limit=10, ref_ids=ref_ids)
            ]
            conn.rollback()
        assert reclaimable == [r for r in ref_ids if r not in reached]


# ── re-match sweep for failure-stranded entries ─────────────────────────


def _stamp(store: Any, ref_id: int) -> None:
    with store.pool.connection() as conn:
        bib_parse_mod._stamp_paper_version(conn, ref_id)
        conn.commit()


def _set_conf(store: Any, entry_id: int, conf: float) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE paper_bib_entries SET match_conf = %s WHERE id = %s",
            (conf, entry_id),
        )
        conn.commit()


def _crossref_hit(monkeypatch: pytest.MonkeyPatch, items: list[Any]) -> list[str]:
    """Patch ``_crossref_query`` to return ``items``; returns the call log."""
    calls: list[str] = []

    def fake(raw_text: str, *, mailto: str = "") -> Any:
        calls.append(raw_text)
        return items

    monkeypatch.setattr(bib_parse_mod, "_crossref_query", fake)
    return calls


_HIT = [
    {
        "DOI": "10.1/zzz",
        "score": 90.0,
        "title": ["A Paper"],
        "container-title": ["J"],
        "issued": {"date-parts": [[2020]]},
    }
]


class TestRematchSweep:
    def test_rematches_failure_null_entry_on_stamped_paper(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref_id = seed_ref(store, title="stamped")
        entry_id = _insert_entry(store, ref_id, 1, "stranded citation, J, 2020.")
        _stamp(store, ref_id)
        calls = _crossref_hit(monkeypatch, _HIT)

        out = bib_parse_mod.run_bib_rematch_sweep(store, client=_FakeClient("{}"))

        assert out == {
            "selected": 1,
            "matched": 1,
            "unmatched": 0,
            "failed": 0,
            "rate_limited": 0,
        }
        assert len(calls) == 1
        doi, _s2, _held, conf = _entry_row(store, entry_id)
        assert doi == "10.1/zzz"
        assert conf == pytest.approx(0.8)

    def test_still_failing_entry_stays_null(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref_id = seed_ref(store, title="stamped")
        entry_id = _insert_entry(store, ref_id, 1, "stranded citation, J, 2020.")
        _stamp(store, ref_id)
        monkeypatch.setattr(bib_parse_mod, "_crossref_query", lambda *_a, **_k: None)
        out = bib_parse_mod.run_bib_rematch_sweep(store, client=_FakeClient("{}"))
        assert out["selected"] == 1 and out["failed"] == 1
        assert _entry_row(store, entry_id)[3] is None

    def test_never_requeries_resolved_entries(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref_id = seed_ref(store, title="stamped")
        no_match = _insert_entry(store, ref_id, 1, "true no match, J, 2020.")
        matched = _insert_entry(store, ref_id, 2, "already matched, J, 2021.")
        _set_conf(store, no_match, 0.0)
        _set_conf(store, matched, 0.8)
        _stamp(store, ref_id)
        calls = _crossref_hit(monkeypatch, _HIT)

        out = bib_parse_mod.run_bib_rematch_sweep(store, client=_FakeClient("{}"))

        assert out["selected"] == 0
        assert calls == []
        assert _entry_row(store, no_match)[3] == 0.0
        assert _entry_row(store, matched)[3] == pytest.approx(0.8)

    def test_skips_unstamped_and_leased_papers(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        unstamped = seed_ref(store, title="unstamped")
        _insert_entry(store, unstamped, 1, "citation on unstamped, J, 2020.")
        leased = seed_ref(store, title="leased")
        _insert_entry(store, leased, 1, "citation on leased, J, 2020.")
        _stamp(store, leased)
        with store.pool.connection() as conn:
            ref_lease.stamp_attempt(store, leased, bib_parse_mod._LEASE_NS, conn=conn)
            conn.commit()
        calls = _crossref_hit(monkeypatch, _HIT)

        out = bib_parse_mod.run_bib_rematch_sweep(store, client=_FakeClient("{}"))

        assert out["selected"] == 0
        assert calls == []

    def test_respects_limit(self, store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        ref_id = seed_ref(store, title="stamped")
        ids = [
            _insert_entry(store, ref_id, m, f"stranded citation {m}, J, 2020.")
            for m in range(1, 5)
        ]
        _stamp(store, ref_id)
        calls = _crossref_hit(monkeypatch, [])

        out = bib_parse_mod.run_bib_rematch_sweep(
            store, client=_FakeClient("{}"), limit=2
        )

        assert out["selected"] == 2 and out["unmatched"] == 2
        assert len(calls) == 2
        resolved = [i for i in ids if _entry_row(store, i)[3] is not None]
        assert len(resolved) == 2

    def test_second_run_is_noop_with_zero_crossref_calls(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref_id = seed_ref(store, title="stamped")
        _insert_entry(store, ref_id, 1, "stranded one, J, 2020.")
        _insert_entry(store, ref_id, 2, "stranded two, J, 2021.")
        _stamp(store, ref_id)
        calls = _crossref_hit(monkeypatch, [])
        bib_parse_mod.run_bib_rematch_sweep(store, client=_FakeClient("{}"))
        assert len(calls) == 2

        out = bib_parse_mod.run_bib_rematch_sweep(store, client=_FakeClient("{}"))

        assert out["selected"] == 0
        assert len(calls) == 2

    def test_rate_limited_stops_sweep_and_leaves_null(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref_id = seed_ref(store, title="stamped")
        ids = [
            _insert_entry(store, ref_id, m, f"stranded citation {m}, J, 2020.")
            for m in range(1, 4)
        ]
        _stamp(store, ref_id)
        refusals: list[str] = []

        def refuse(provider: str, **_: Any) -> None:
            refusals.append(provider)
            _refuse(provider)

        def _no_http(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("safe_get fired despite a limiter refusal")

        monkeypatch.setattr("precis.utils.rate_limit.require", refuse)
        monkeypatch.setattr("precis.utils.safe_fetch.safe_get", _no_http)

        out = bib_parse_mod.run_bib_rematch_sweep(store, client=_FakeClient("{}"))

        assert out["selected"] == 3 and out["rate_limited"] == 1
        assert len(refusals) == 1  # stopped at the first refusal
        assert all(_entry_row(store, i)[3] is None for i in ids)


class TestCycleWiring:
    def test_sweep_runs_only_when_nothing_claimed(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sweeps: list[int] = []

        def _sweep(*_a: Any, **_k: Any) -> dict[str, int]:
            sweeps.append(1)
            return {}

        monkeypatch.setattr(bib_parse_mod, "run_bib_rematch_sweep", _sweep)
        claimed = {"claimed": 2, "ok": 2, "failed": 0}
        monkeypatch.setattr(
            bib_parse_mod, "run_bib_parse_pass", lambda *a, **k: claimed
        )
        r = bib_parse_mod.run_bib_parse_cycle(store, client=_FakeClient("{}"))
        assert r == claimed and sweeps == []

        drained = {"claimed": 0, "ok": 0, "failed": 0}
        monkeypatch.setattr(
            bib_parse_mod, "run_bib_parse_pass", lambda *a, **k: drained
        )
        r = bib_parse_mod.run_bib_parse_cycle(store, client=_FakeClient("{}"))
        assert r == drained and sweeps == [1]

    def test_sweep_failure_keeps_claim_pass_counts(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("sweep exploded")

        drained = {"claimed": 0, "ok": 0, "failed": 0}
        monkeypatch.setattr(bib_parse_mod, "run_bib_rematch_sweep", boom)
        monkeypatch.setattr(
            bib_parse_mod, "run_bib_parse_pass", lambda *a, **k: drained
        )
        r = bib_parse_mod.run_bib_parse_cycle(store, client=_FakeClient("{}"))
        assert r == drained


# ── cites-link minting + one-shot backfill (gr513755 slice 1) ─────────


def _cites_links(
    store: Any, *, src: int | None = None, dst: int | None = None
) -> list[tuple[Any, ...]]:
    """``links`` rows for ``relation='cites'``, optionally endpoint-scoped."""
    sql = (
        "SELECT src_ref_id, dst_ref_id, src_chunk_id, dst_chunk_id, "
        "set_by, meta FROM links WHERE relation = 'cites'"
    )
    params: list[Any] = []
    if src is not None:
        sql += " AND src_ref_id = %s"
        params.append(src)
    if dst is not None:
        sql += " AND dst_ref_id = %s"
        params.append(dst)
    sql += " ORDER BY link_id"
    with store.pool.connection() as conn:
        return [tuple(r) for r in conn.execute(sql, params).fetchall()]


def _seed_matched_entry(
    store: Any, ref_id: int, marker: int, held_ref_id: int | None
) -> int:
    """A ``paper_bib_entries`` row as it looks after a successful match."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO paper_bib_entries (ref_id, marker, raw_text, doi, "
            "held_ref_id, match_conf, parse_version) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                ref_id,
                marker,
                "A matched citation, J, 2020.",
                "10.9/held",
                held_ref_id,
                0.8,
                BIB_PARSE_VERSION,
            ),
        ).fetchone()
        conn.commit()
    assert row is not None
    return int(row[0])


def _retire(store: Any, ref_id: int) -> None:
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET retired_at = now() WHERE ref_id = %s", (ref_id,))
        conn.commit()


class TestBibCitesLinks:
    def test_local_doi_match_mints_exactly_one_cites_link(
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
        entry_id = _insert_entry(
            store,
            ref_id,
            1,
            "Some Author, ChemCatChem 2020, 12, 360, "
            "https://doi.org/10.1002/cctc.201901234.",
        )
        monkeypatch.setattr(
            "precis.utils.safe_fetch.safe_get",
            lambda *_a, **_k: pytest.fail("local hit must not query Crossref"),
        )

        run_bib_parse_match_pass(store, ref_id, client=_FakeClient("{}"))

        links = _cites_links(store, src=ref_id)
        assert len(links) == 1
        _src, dst, src_chunk, dst_chunk, set_by, meta = links[0]
        assert dst == held_id
        assert src_chunk is None and dst_chunk is None  # paper-level edge
        assert set_by == "system"
        assert meta["source"] == "bib_parse"
        assert meta["bib_entry_id"] == entry_id

    def test_crossref_match_mints_cites_link(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref_id = seed_ref(store, title="citing paper")
        held_id = seed_ref(store, title="the cited paper")
        _add_id(store, held_id, "doi", "10.1/zzz")
        _insert_entry(store, ref_id, 1, "A citation matched via Crossref.")
        _crossref_hit(monkeypatch, _HIT)

        run_bib_parse_match_pass(store, ref_id, client=_FakeClient("{}"))

        links = _cites_links(store, src=ref_id, dst=held_id)
        assert len(links) == 1

    def test_rematch_sweep_match_mints_cites_link(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref_id = seed_ref(store, title="stamped citing paper")
        held_id = seed_ref(store, title="the cited paper")
        _add_id(store, held_id, "doi", "10.1/zzz")
        _insert_entry(store, ref_id, 1, "A stranded citation matched by sweep.")
        _stamp(store, ref_id)
        _crossref_hit(monkeypatch, _HIT)

        out = bib_parse_mod.run_bib_rematch_sweep(store, client=_FakeClient("{}"))

        assert out["matched"] == 1
        assert len(_cites_links(store, src=ref_id, dst=held_id)) == 1

    def test_ensure_is_idempotent(self, store: Any) -> None:
        ref_id = seed_ref(store, title="citing paper")
        held_id = seed_ref(store, title="the cited paper")
        entry_id = _seed_matched_entry(store, ref_id, 1, held_id)

        with store.pool.connection() as conn:
            first = bib_parse_mod.ensure_bib_cites_links(
                store, ref_id, [(entry_id, held_id)], conn=conn
            )
            conn.commit()
        with store.pool.connection() as conn:
            second = bib_parse_mod.ensure_bib_cites_links(
                store, ref_id, [(entry_id, held_id)], conn=conn
            )
            conn.commit()

        assert first == 1 and second == 1  # ensured both times
        assert len(_cites_links(store, src=ref_id, dst=held_id)) == 1

    def test_distinct_targets_one_link_each_shared_target_collapses(
        self, store: Any
    ) -> None:
        ref_id = seed_ref(store, title="citing paper")
        held_a = seed_ref(store, title="cited A")
        held_b = seed_ref(store, title="cited B")
        e1 = _seed_matched_entry(store, ref_id, 1, held_a)
        e2 = _seed_matched_entry(store, ref_id, 2, held_a)  # dup target
        e3 = _seed_matched_entry(store, ref_id, 3, held_b)

        with store.pool.connection() as conn:
            bib_parse_mod.ensure_bib_cites_links(
                store,
                ref_id,
                [(e1, held_a), (e2, held_a), (e3, held_b)],
                conn=conn,
            )
            conn.commit()

        links = _cites_links(store, src=ref_id)
        assert len(links) == 2
        a_link = next(lnk for lnk in links if lnk[1] == held_a)
        assert a_link[5]["bib_entry_id"] == min(e1, e2)

    def test_self_cite_skipped(self, store: Any) -> None:
        ref_id = seed_ref(store, title="self-citing paper")
        entry_id = _seed_matched_entry(store, ref_id, 1, ref_id)

        with store.pool.connection() as conn:
            ensured = bib_parse_mod.ensure_bib_cites_links(
                store, ref_id, [(entry_id, ref_id)], conn=conn
            )
            conn.commit()

        assert ensured == 0
        assert _cites_links(store, src=ref_id) == []

    def test_self_cite_through_match_path(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A bibliography line naming the citing paper itself: the match
        # records held_ref_id = ref_id; no self-loop edge may be minted.
        ref_id = seed_ref(store, title="citing paper")
        _add_id(store, ref_id, "doi", "10.1002/self")
        store.replace_s2_neighbors(
            ref_id,
            "cites",
            [
                {
                    "s2_id": "SELF",
                    "doi": "10.1002/self",
                    "title": "T",
                    "year": 2020,
                    "held_ref_id": None,
                }
            ],
        )
        entry_id = _insert_entry(
            store, ref_id, 1, "Self Cite, J 2020, https://doi.org/10.1002/self."
        )
        monkeypatch.setattr(
            "precis.utils.safe_fetch.safe_get",
            lambda *_a, **_k: pytest.fail("local hit must not query Crossref"),
        )

        run_bib_parse_match_pass(store, ref_id, client=_FakeClient("{}"))

        assert _entry_row(store, entry_id)[2] == ref_id  # held = self
        assert _cites_links(store, src=ref_id) == []

    def test_retired_target_skipped(self, store: Any) -> None:
        # held_ref_id stamped while the target was live; retired before the
        # link was minted (the path the backfill depends on).
        ref_id = seed_ref(store, title="citing paper")
        held_id = seed_ref(store, title="cited, since retired")
        entry_id = _seed_matched_entry(store, ref_id, 1, held_id)
        _retire(store, held_id)

        with store.pool.connection() as conn:
            ensured = bib_parse_mod.ensure_bib_cites_links(
                store, ref_id, [(entry_id, held_id)], conn=conn
            )
            conn.commit()

        assert ensured == 0
        assert _cites_links(store, src=ref_id) == []


class TestBackfillBibCites:
    def test_dry_run_counts_then_apply_writes(self, store: Any) -> None:
        from precis.backfill.bib_cites import backfill_bib_cites

        c1 = seed_ref(store, title="citer one")
        c2 = seed_ref(store, title="citer two")
        h1 = seed_ref(store, title="held one")
        h2 = seed_ref(store, title="held two")
        e1 = _seed_matched_entry(store, c1, 1, h1)
        _seed_matched_entry(store, c1, 2, h1)  # same pair, collapsed
        e3 = _seed_matched_entry(store, c1, 3, h2)
        e4 = _seed_matched_entry(store, c2, 1, h1)
        _seed_matched_entry(store, c2, 2, None)  # unmatched — never a pair

        dry = backfill_bib_cites(store, apply=False)
        assert dry.pairs_missing == 3
        assert dry.papers == 2
        assert _cites_links(store) == []  # dry-run wrote nothing

        applied = backfill_bib_cites(store, apply=True)
        assert applied.links_ensured == 3
        links = _cites_links(store)
        assert len(links) == 3
        by_dst = {(lnk[0], lnk[1]): lnk for lnk in links}
        assert by_dst[(c1, h1)][5]["bib_entry_id"] == e1
        assert by_dst[(c1, h2)][5]["bib_entry_id"] == e3
        assert by_dst[(c2, h1)][5]["bib_entry_id"] == e4
        assert all(lnk[4] == "system" for lnk in links)

        rerun = backfill_bib_cites(store, apply=False)
        assert rerun.pairs_missing == 0

    def test_existing_link_not_recounted_and_meta_untouched(self, store: Any) -> None:
        from precis.backfill.bib_cites import backfill_bib_cites

        c1 = seed_ref(store, title="citer")
        h1 = seed_ref(store, title="held")
        _seed_matched_entry(store, c1, 1, h1)
        with store.pool.connection() as conn:
            store.add_link(
                src_ref_id=c1,
                dst_ref_id=h1,
                relation="cites",
                set_by="agent",
                meta={"via": "backfill:citation-graph"},
                conn=conn,
            )
            conn.commit()

        before = _cites_links(store, src=c1, dst=h1)[0]
        result = backfill_bib_cites(store, apply=True)

        assert result.pairs_missing == 0
        assert result.links_ensured == 0
        after = _cites_links(store, src=c1, dst=h1)
        assert len(after) == 1
        assert after[0][5] == before[5]  # first writer's meta wins

    def test_self_cite_and_retired_target_excluded_from_dry_run(
        self, store: Any
    ) -> None:
        from precis.backfill.bib_cites import backfill_bib_cites

        c1 = seed_ref(store, title="citer")
        live = seed_ref(store, title="held, live")
        dead = seed_ref(store, title="held, retired")
        _seed_matched_entry(store, c1, 1, c1)  # self-cite
        _seed_matched_entry(store, c1, 2, dead)
        _seed_matched_entry(store, c1, 3, live)
        _retire(store, dead)

        dry = backfill_bib_cites(store, apply=False)
        assert dry.pairs_missing == 1

        applied = backfill_bib_cites(store, apply=True)
        assert applied.links_ensured == 1
        links = _cites_links(store, src=c1)
        assert len(links) == 1 and links[0][1] == live

    def test_limit_caps_selected_pairs(self, store: Any) -> None:
        from precis.backfill.bib_cites import backfill_bib_cites

        c1 = seed_ref(store, title="citer")
        for i in range(3):
            _seed_matched_entry(store, c1, i + 1, seed_ref(store, title=f"h{i}"))

        dry = backfill_bib_cites(store, apply=False, limit=2)
        assert dry.pairs_missing == 2
        applied = backfill_bib_cites(store, apply=True, limit=2)
        assert applied.links_ensured == 2
        assert len(_cites_links(store, src=c1)) == 2

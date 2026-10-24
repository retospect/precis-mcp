"""The consumer's decision record (gr512124) and the paper identity
gate (gr512125).

DB-backed (``store`` fixture), never prod. Paper bodies are synthetic
word sequences (``w123 w45 …``) — fixtures carry no paper text. The
shapes rebuild what the 2026-10-24 audit found on prod: a title/author
match with ``same_pdf=false`` and no DOI on one side, an SI twin
(``pdf_role='supplement'``, "Supporting Information:" title, ``.s001``
DOI), an arXiv copy against its journal version, and a corrigendum.
"""

from __future__ import annotations

import json
import random
from typing import Any

import pytest

from precis.embedder import MockEmbedder
from precis.graph.garden.apply import MergeRefused, apply_merge
from precis.graph.garden.candidates import GardenCandidate
from precis.graph.garden.consumer import (
    CONSUMER_ACTOR,
    ConsumerVerdict,
    consume_proposals,
)
from precis.graph.garden.paper_gate import (
    NO_EVIDENCE_REASON,
    TEXT_OVERLAP_MIN,
    paper_identity,
)
from precis.graph.garden.proposals import emit_proposal, pending_proposals
from precis.store import Store

_EMB = MockEmbedder(dim=1024)
_TITLE = "Hydrogen Absorption in Palladium Nanocubes Under Strain"


def _sha(store: Store, ref_id: int) -> str:
    with store.pool.connection() as conn:
        row = conn.execute("SELECT precis_target_sha('ref', %s)", (ref_id,)).fetchone()
    assert row is not None and row[0] is not None
    return str(row[0])


def _words(seed: int, n: int = 600) -> list[str]:
    rng = random.Random(seed)
    return [f"w{rng.randrange(5000)}" for _ in range(n)]


def _paper(
    store: Store,
    slug: str,
    title: str = _TITLE,
    *,
    doi: str | None = None,
    arxiv: str | None = None,
    journal: str | None = None,
    pdf_sha: str | None = None,
    pdf_role: str = "main",
    body: list[str] | None = None,
) -> int:
    rid = int(
        store.insert_ref(
            kind="paper",
            slug=slug,
            title=title,
            year=2020,
            authors=[{"family": "Smith", "given": "A."}],
            meta={"journal": journal} if journal else {},
        ).id
    )
    with store.pool.connection() as conn:
        if pdf_sha:
            conn.execute(
                "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
                "size_bytes, storage_path) VALUES (%s, %s, 1, 100, '') "
                "ON CONFLICT (pdf_sha256) DO NOTHING",
                (pdf_sha, pdf_sha),
            )
        conn.execute(
            "UPDATE refs SET pdf_role = %s, pdf_sha256 = %s WHERE ref_id = %s",
            (pdf_role, pdf_sha, rid),
        )
        for kind, value in (("doi", doi), ("arxiv", arxiv)):
            if value:
                conn.execute(
                    "INSERT INTO ref_identifiers (id_kind, id_value, ref_id) "
                    "VALUES (%s, %s, %s)",
                    (kind, value, rid),
                )
        words = body or []
        for i in range(0, len(words), 60):
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, %s, 'paragraph', %s)",
                (rid, i // 60, " ".join(words[i : i + 60])),
            )
    return rid


def _proposal(
    store: Store, winner: int, loser: int, *, detector: str = "paper_identity"
) -> int:
    rid = emit_proposal(
        store,
        GardenCandidate(
            detector=detector,
            action="merge",
            target_kind="ref",
            target_id=loser,
            other_id=winner,
            title=f"paper identity {winner}↔{loser}",
            evidence={"doi": "missing", "same_pdf": False, "years": [2020, 2020]},
            payload={
                "loser": loser,
                "winner": winner,
                "kind": "paper",
                "shas": {
                    str(loser): _sha(store, loser),
                    str(winner): _sha(store, winner),
                },
            },
        ),
        rationale="Identical title and same first author",
        model="castor",
    )
    assert rid is not None
    return rid


class _Judge:
    def __init__(self, verdict: str = "approve", reason: str = "") -> None:
        self.verdict, self.reason = verdict, reason
        self.seen: list[int] = []

    def __call__(self, _store: Store, prop: Any) -> ConsumerVerdict:
        self.seen.append(prop.review_id)
        return ConsumerVerdict(
            verdict=self.verdict, reason=self.reason, why="judged", model="opus"
        )


def _decisions(store: Store, review_id: int) -> list[dict[str, Any]]:
    """Every row resolving ``review_id``: actor, verdict, model + garden."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT actor, verdict, model, note FROM reviews "
            "WHERE note LIKE %s ORDER BY review_id",
            (f'%"resolves": {review_id}, %',),
        ).fetchall()
    return [
        {"actor": a, "verdict": v, "model": m, **json.loads(n)["garden"]}
        for a, v, m, n in rows
    ]


def _holds(store: Store, review_id: int) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT note FROM reviews WHERE actor = %s AND note LIKE %s",
            (CONSUMER_ACTOR, '%"garden_review"%'),
        ).fetchall()
    out = [json.loads(n)["garden_review"] for (n,) in rows]
    return [g for g in out if g.get("proposal") == review_id]


def _live(store: Store, ref_id: int) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT retired_at IS NULL FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return bool(row and row[0])


# ── gr512124: one decision row per decided proposal ─────────────────────────


class TestDecisionRecord:
    def test_applied_merge_leaves_exactly_one_applied_row(self, store: Store) -> None:
        a = int(store.insert_ref(kind="concept", slug=None, title="band gap").id)
        b = int(store.insert_ref(kind="concept", slug=None, title="band-gap").id)
        for rid in (a, b):
            cid = store.chunks.upsert_card_combined(rid, "band gap")
            with store.pool.connection() as conn:
                conn.execute(
                    "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, "
                    "status, attempts) VALUES (%s, 'bge-m3', %s, 'ok', 1)",
                    (cid, _EMB.embed_one("band gap")),
                )
        review_id = emit_proposal(
            store,
            GardenCandidate(
                detector="dup_pair",
                action="merge",
                target_kind="ref",
                target_id=b,
                other_id=a,
                title="dup pair concept",
                evidence={"kind": "concept", "distance": 0.0},
                payload={
                    "loser": b,
                    "winner": a,
                    "kind": "concept",
                    "shas": {str(a): _sha(store, a), str(b): _sha(store, b)},
                },
            ),
            rationale="same term",
        )
        assert review_id is not None
        report = consume_proposals(store, review=_Judge("approve"))
        assert report.applied == 1
        (row,) = _decisions(store, review_id)
        assert (row["actor"], row["verdict"], row["model"]) == (
            CONSUMER_ACTOR,
            "approved",
            "opus",
        )
        assert row["outcome"] == "applied"
        assert (row["resolves"], row["loser"], row["winner"]) == (review_id, b, a)
        assert row["applied"] == report.merges[0]
        assert row["evidence"] == {"kind": "concept", "distance": 0.0}
        assert (row["why"], row["by_model"], row["detector"]) == (
            "judged",
            "opus",
            "dup_pair",
        )
        # A second pass decides nothing more.
        consume_proposals(store, review=_Judge("approve"))
        assert len(_decisions(store, review_id)) == 1

    def test_rejected_merge_row_names_the_pair_and_evidence(self, store: Store) -> None:
        a = int(store.insert_ref(kind="concept", slug=None, title="unit cost").id)
        b = int(store.insert_ref(kind="concept", slug=None, title="cost per mass").id)
        review_id = emit_proposal(
            store,
            GardenCandidate(
                detector="dup_pair",
                action="merge",
                target_kind="ref",
                target_id=b,
                other_id=a,
                evidence={"kind": "concept", "distance": 0.2},
                payload={
                    "loser": b,
                    "winner": a,
                    "shas": {str(a): _sha(store, a), str(b): _sha(store, b)},
                },
            ),
            rationale="close",
        )
        assert review_id is not None
        consume_proposals(store, review=_Judge("reject", "specialisation"))
        (row,) = _decisions(store, review_id)
        assert (row["verdict"], row["outcome"], row["reason"]) == (
            "rejected",
            "rejected",
            "specialisation",
        )
        assert (row["loser"], row["winner"], row["model"]) == (b, a, "opus")
        assert row["evidence"] == {"kind": "concept", "distance": 0.2}


# ── gr512125: the paper identity gate ───────────────────────────────────────


class TestPaperGate:
    def test_title_author_only_is_held_not_applied(self, store: Store) -> None:
        """The prod shape: one side has the journal DOI, the other none,
        different PDFs, different bodies — the judge is never asked."""
        w = _paper(
            store,
            "w",
            doi="10.1016/j.x.2026.1",
            journal="Physics Letters A",
            pdf_sha="a" * 64,
            body=_words(1),
        )
        lo = _paper(store, "l", pdf_sha="b" * 64, body=_words(2))
        review_id = _proposal(store, w, lo)
        judge = _Judge("approve")
        report = consume_proposals(store, review=judge)
        assert judge.seen == []
        assert (report.applied, report.held, report.rejected) == (0, 1, 0)
        assert _live(store, lo) and _live(store, w)
        assert [p.review_id for p in pending_proposals(store)] == [review_id]
        (hold,) = _holds(store, review_id)
        assert hold["held"] == NO_EVIDENCE_REASON
        assert hold["identity"]["positive"] == []
        consume_proposals(store, review=judge)
        assert judge.seen == [] and len(_holds(store, review_id)) == 1

    def test_same_pdf_applies(self, store: Store) -> None:
        w = _paper(store, "w", pdf_sha="c" * 64)
        lo = _paper(store, "l", pdf_sha="c" * 64)
        review_id = _proposal(store, w, lo)
        report = consume_proposals(store, review=_Judge("approve"))
        assert report.applied == 1 and not _live(store, lo)
        (row,) = _decisions(store, review_id)
        assert row["identity"]["positive"] == ["same_pdf"]

    def test_body_text_overlap_applies_and_names_the_score(self, store: Store) -> None:
        text = _words(7)
        w = _paper(store, "w", pdf_sha="d" * 64, body=text)
        lo = _paper(store, "l", pdf_sha="e" * 64, body=text[:-12] + _words(8, 12))
        review_id = _proposal(store, w, lo)
        report = consume_proposals(store, review=_Judge("approve"))
        assert report.applied == 1
        (row,) = _decisions(store, review_id)
        ident = row["identity"]
        assert ident["positive"] == ["text_overlap"]
        assert ident["text_overlap"] >= TEXT_OVERLAP_MIN

    def test_partial_overlap_under_the_floor_is_held(self, store: Store) -> None:
        """Half the text shared (a revised version) scores ~0.33."""
        w = _paper(store, "w", body=_words(9))
        lo = _paper(store, "l", body=_words(9)[:300] + _words(10, 300))
        _proposal(store, w, lo)
        with store.pool.connection() as conn:
            ident = paper_identity(store, lo, w, conn=conn)
        assert ident.text_overlap is not None
        assert ident.text_overlap < TEXT_OVERLAP_MIN
        assert ident.reason == NO_EVIDENCE_REASON
        report = consume_proposals(store, review=_Judge("approve"))
        assert (report.applied, report.held) == (0, 1)

    @pytest.mark.parametrize(
        "loser_kwargs",
        [
            {"pdf_role": "supplement"},
            {"title": "Supporting Information: " + _TITLE},
            {"doi": "10.1021/acs.est.8b01128.s001"},
        ],
        ids=["pdf_role", "title_marker", "component_doi"],
    )
    def test_supplement_shapes_are_refused(
        self, store: Store, loser_kwargs: dict[str, Any]
    ) -> None:
        w = _paper(store, "w", doi="10.1021/acs.est.8b01128", pdf_sha="f" * 64)
        lo = _paper(store, "l", pdf_sha="f" * 64, **loser_kwargs)  # same PDF even
        review_id = _proposal(store, w, lo)
        judge = _Judge("approve")
        report = consume_proposals(store, review=judge)
        assert judge.seen == [] and report.applied == 0
        (row,) = _decisions(store, review_id)
        assert (row["verdict"], row["reason"]) == ("rejected", "supplement")
        assert _live(store, lo)

    @pytest.mark.parametrize(
        ("winner_kwargs", "loser_kwargs"),
        [
            # prod review 8504: the arXiv copy carries the journal
            # copied from its published version, on BOTH sides
            (
                {"journal": "Neural Information Processing Systems"},
                {
                    "journal": "Neural Information Processing Systems",
                    "arxiv": "1310.4546",
                },
            ),
            # arXiv copy (identifier only) vs the journal version
            (
                {"journal": "Neural Information Processing Systems"},
                {"arxiv": "1310.4546"},
            ),
            # preprint-server DOI vs a journal DOI
            (
                {"doi": "10.1016/j.jcp.2026.115240"},
                {"doi": "10.48550/arxiv.2601.05050"},
            ),
        ],
        ids=[
            "prod_8504_copied_journal",
            "arxiv_vs_journal",
            "preprint_doi_vs_journal_doi",
        ],
    )
    def test_version_pairs_are_refused(
        self,
        store: Store,
        winner_kwargs: dict[str, Any],
        loser_kwargs: dict[str, Any],
    ) -> None:
        """Refused even with identical body text — a near-identical
        preprint is still a distinct record."""
        text = _words(11)
        w = _paper(store, "w", body=text, **winner_kwargs)
        lo = _paper(store, "l", body=text, **loser_kwargs)
        review_id = _proposal(store, w, lo)
        judge = _Judge("approve")
        consume_proposals(store, review=judge)
        assert judge.seen == []
        (row,) = _decisions(store, review_id)
        assert (row["verdict"], row["reason"]) == ("rejected", "version_pair")
        assert _live(store, lo)

    @pytest.mark.parametrize(
        ("winner_kwargs", "loser_kwargs", "held"),
        [
            # two venues, same text, no shared DOI/PDF: ambiguous
            (
                {"journal": "Physical Review B"},
                {"journal": "Physical Review Letters"},
                "venue_differs",
            ),
            # prod 8427 shape: arXiv DOI on one side, nothing on the other
            ({}, {"doi": "10.48550/arxiv.2603.29152"}, "version_unclear"),
        ],
        ids=["venues_differ", "preprint_vs_bare"],
    )
    def test_ambiguous_pairs_are_held_not_closed(
        self,
        store: Store,
        winner_kwargs: dict[str, Any],
        loser_kwargs: dict[str, Any],
        held: str,
    ) -> None:
        """A rejected key stays closed while the target is unchanged, so
        an ambiguous verdict holds for a human instead."""
        text = _words(12)
        w = _paper(store, "w", body=text, **winner_kwargs)
        lo = _paper(store, "l", body=text, **loser_kwargs)
        review_id = _proposal(store, w, lo)
        judge = _Judge("approve")
        report = consume_proposals(store, review=judge)
        assert judge.seen == [] and (report.held, report.rejected) == (1, 0)
        assert _decisions(store, review_id) == []
        assert [p.review_id for p in pending_proposals(store)] == [review_id]
        (hold,) = _holds(store, review_id)
        assert hold["held"] == held

    @pytest.mark.parametrize(
        ("winner_journal", "loser_journal"),
        [
            ("Phys. Rev. B", "Physical Review B"),
            ("J. Chem. Phys.", "The Journal of Chemical Physics"),
        ],
    )
    def test_same_pdf_wins_over_a_journal_abbreviation(
        self, store: Store, winner_journal: str, loser_journal: str
    ) -> None:
        w = _paper(store, "w", journal=winner_journal, pdf_sha="9" * 64)
        lo = _paper(store, "l", journal=loser_journal, pdf_sha="9" * 64)
        review_id = _proposal(store, w, lo)
        report = consume_proposals(store, review=_Judge("approve"))
        assert report.applied == 1 and not _live(store, lo)
        (row,) = _decisions(store, review_id)
        assert row["identity"]["positive"] == ["same_pdf"]

    def test_same_pdf_wins_over_different_journal_strings(self, store: Store) -> None:
        """Byte-identical PDFs are one document whatever the venue
        strings say — never refused, never held on the venue."""
        w = _paper(store, "w", journal="Phys. Rev. B", pdf_sha="8" * 64)
        lo = _paper(store, "l", journal="Physical Review Letters", pdf_sha="8" * 64)
        assert paper_identity(store, lo, w).passes

    def test_abbreviated_venue_with_text_overlap_applies(self, store: Store) -> None:
        text = _words(13)
        w = _paper(store, "w", journal="J. Chem. Phys.", body=text)
        lo = _paper(store, "l", journal="The Journal of Chemical Physics", body=text)
        ident = paper_identity(store, lo, w)
        assert ident.passes and ident.positive == ["text_overlap"]

    def test_missing_endpoint_is_target_gone(self, store: Store) -> None:
        w = _paper(store, "w")
        ident = paper_identity(store, 2_000_000_000, w)
        assert (ident.refused, ident.passes) == ("target_gone", False)

    def test_corrigendum_title_is_refused(self, store: Store) -> None:
        w = _paper(store, "w", pdf_sha="g" * 64)
        lo = _paper(store, "l", "Corrigendum: " + _TITLE, pdf_sha="g" * 64)
        review_id = _proposal(store, w, lo)
        consume_proposals(store, review=_Judge("approve"))
        (row,) = _decisions(store, review_id)
        assert (row["verdict"], row["reason"]) == ("rejected", "corrigendum")

    def test_corrects_edge_is_refused(self, store: Store) -> None:
        w = _paper(store, "w", pdf_sha="h" * 64)
        lo = _paper(store, "l", pdf_sha="h" * 64)
        store.add_link(src_ref_id=lo, dst_ref_id=w, relation="corrects")
        review_id = _proposal(store, w, lo)
        consume_proposals(store, review=_Judge("approve"))
        (row,) = _decisions(store, review_id)
        assert (row["verdict"], row["reason"]) == ("rejected", "corrigendum")

    def test_apply_door_regates_inside_the_transaction(self, store: Store) -> None:
        """The machine door refuses a title-only pair even when called
        directly; a human applying by hand (no gate) is not refused."""
        w = _paper(store, "w", doi="10.1/abc")
        lo = _paper(store, "l")
        review_id = _proposal(store, w, lo)
        with pytest.raises(MergeRefused) as exc:
            apply_merge(
                store,
                lo,
                w,
                actor=CONSUMER_ACTOR,
                review_id=review_id,
                identity_gate=True,
            )
        assert exc.value.reason == NO_EVIDENCE_REASON
        assert _live(store, lo) and _decisions(store, review_id) == []
        apply_merge(store, lo, w, actor="reto", review_id=review_id)
        assert not _live(store, lo)

    def test_non_paper_merges_are_not_gated(self, store: Store) -> None:
        a = int(store.insert_ref(kind="concept", slug=None, title="x").id)
        b = int(store.insert_ref(kind="concept", slug=None, title="y").id)
        prop = _proposal(store, a, b, detector="dup_pair")
        judge = _Judge("reject", "distinct_terms")
        report = consume_proposals(store, review=judge)
        assert judge.seen == [prop] and report.held == 0

    def test_apply_time_hold_is_held_not_closed(self, store: Store) -> None:
        """The pre-model gate passes on text overlap; while the model
        judges, an enrichment pass files an arXiv id on the loser (not
        part of the ref sha, so the pins still hold). The
        in-transaction gate now sees a preprint id against a bare side —
        held like the pre-model path, not closed."""
        text = _words(14)
        w = _paper(store, "w", body=text)
        lo = _paper(store, "l", body=text)
        review_id = _proposal(store, w, lo)

        def _judge_while_enriched(s: Store, prop: Any) -> ConsumerVerdict:
            with s.pool.connection() as conn:
                conn.execute(
                    "INSERT INTO ref_identifiers (id_kind, id_value, ref_id) "
                    "VALUES ('arxiv', '2601.00001', %s)",
                    (lo,),
                )
            return ConsumerVerdict(verdict="approve", why="judged", model="opus")

        report = consume_proposals(store, review=_judge_while_enriched)
        assert (report.applied, report.held, report.rejected) == (0, 1, 0)
        assert _live(store, lo) and _decisions(store, review_id) == []
        (hold,) = _holds(store, review_id)
        assert hold["held"] == "version_unclear"
        assert hold["identity"]["hold"] == "version_unclear"

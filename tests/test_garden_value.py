"""Gardener value slice 3 — relink narrowing, prune-redundant, reviewer.

DB-backed (``store`` fixture), same harness as ``test_garden.py``: the
MockEmbedder's distance distribution is binary — identical card text
lands at cosine distance ~0 (well inside the recall floor), unrelated
text near ~1.0 — which is what lets the tests sit on both sides of the
distance constants without fixtures tuned to fractions.
"""

from __future__ import annotations

import json
import math
from typing import Any

from precis.embedder import MockEmbedder
from precis.graph.garden import candidates as cand
from precis.graph.garden.candidates import GardenCandidate
from precis.graph.garden.proposals import (
    emit_proposal,
    pending_proposals,
    proposal_key_pending,
)
from precis.graph.garden.proposer import Verdict
from precis.graph.garden.reviewer import (
    REVIEWER_ACTOR,
    REVIEWER_ACTOR_FABLE,
    ReviewerVerdict,
    agreement_stats,
    review_sample,
)
from precis.graph.garden.runner import run_garden_tick
from precis.store import Store
from precis.store.revision_context import revision_context

_EMB = MockEmbedder(dim=1024)
_EMBEDDER = "bge-m3"  # the migration-seeded default embedder


def _ref(
    store: Store, kind: str, title: str, meta: dict[str, Any] | None = None
) -> int:
    return int(
        store.insert_ref(kind=kind, slug=None, title=title, meta=dict(meta or {})).id
    )


def _card(store: Store, ref_id: int, text: str) -> None:
    """A ``card_combined`` chunk plus its default-embedder vector — what
    the embed worker would have written."""
    cid = store.chunks.upsert_card_combined(ref_id, text)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, "
            "attempts) VALUES (%s, %s, %s, 'ok', 1)",
            (cid, _EMBEDDER, _EMB.embed_one(text)),
        )


# ── relink narrowing: inside the floor retrieval already finds it ──────────


class TestRelinkNarrowing:
    def test_far_orphan_keeps_its_candidate_parent(self, store: Store) -> None:
        start = _ref(store, "taxon", "measurands", meta={"start": True})
        orphan = _ref(store, "taxon", "zorbance", meta={"name": "zorbance"})
        _card(store, start, "measurands — the quantities a campaign tracks")
        _card(store, orphan, "zorbance — quux per unit splort")
        (hit,) = cand.orphan_taxon_candidates(store)
        assert hit.target_id == orphan and hit.other_id == start
        dist = hit.evidence["candidate_parent_distance"]
        assert dist > cand.RELINK_MIN_DISTANCE
        # The vacuous reaches-no-start clause is gone: every candidate is
        # unrooted by construction.
        assert "reaches_no_start" not in hit.evidence
        assert hit.evidence["start_nodes"] == [start]

    def test_close_orphan_is_dropped_as_a_retrieval_restate(self, store: Store) -> None:
        start = _ref(store, "taxon", "measurands", meta={"start": True})
        orphan = _ref(store, "taxon", "measurands twin", meta={})
        # Identical card text → distance ~0, inside the recall floor:
        # +recall already lists the start node for this orphan.
        _card(store, start, "the quantities a campaign tracks")
        _card(store, orphan, "the quantities a campaign tracks")
        assert cand.orphan_taxon_candidates(store) == []

    def test_orphan_without_a_card_vector_stays(self, store: Store) -> None:
        start = _ref(store, "taxon", "measurands", meta={"start": True})
        orphan = _ref(store, "taxon", "unembedded thing", meta={})
        _card(store, start, "measurands — the quantities a campaign tracks")
        (hit,) = cand.orphan_taxon_candidates(store)
        assert hit.target_id == orphan and hit.other_id is None
        assert "candidate_parent_distance" not in hit.evidence
        assert hit.evidence["start_nodes"] == [start]


# ── prune-redundant: machine related-to edges that restate the index ───────


def _card_vec(store: Store, ref_id: int, vec: list[float]) -> None:
    """A ``card_combined`` chunk with an explicit vector (the mutual-top-k
    fixtures need distances the hash mock can't express)."""
    cid = store.chunks.upsert_card_combined(ref_id, "seeded card")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, "
            "attempts) VALUES (%s, %s, %s, 'ok', 1)",
            (cid, _EMBEDDER, vec),
        )


def _unit(angle: float, dim: int, *, ndim: int = 1024) -> list[float]:
    """Unit vector ``cos(angle)·e0 + sin(angle)·e_dim``: cosine distance
    from ``e0`` is ``1 - cos(angle)``."""
    v = [0.0] * ndim
    v[0] = math.cos(angle)
    v[dim] = math.sin(angle)
    return v


def _machine_link(store: Store, a: int, b: int) -> int:
    """A ``related-to`` edge stamped as a machine write."""
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        return int(store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to").id)


def _accept(_store: Store, _cand: GardenCandidate) -> Verdict:
    return Verdict(accept=True, rationale="seeded accept", model="test-model")


class TestPruneRedundant:
    def _neighbourhood(self, store: Store) -> tuple[int, list[int], int]:
        """``a`` at e0; five refs at angle 0.3 (dist ~0.045 from a), ``b``
        at angle 0.8 (~0.30 — inside the distance floor, but ranked sixth
        for ``a``, outside the mutual top-5)."""
        a = _ref(store, "concept", "node a")
        _card_vec(store, a, _unit(0.0, 1))
        close = []
        for i in range(1, 6):
            r = _ref(store, "concept", f"close {i}")
            _card_vec(store, r, _unit(0.3, i))
            close.append(r)
        b = _ref(store, "concept", "node b")
        _card_vec(store, b, _unit(0.8, 6))
        return a, close, b

    def test_mutual_machine_pair_proposed(self, store: Store) -> None:
        a, close, _b = self._neighbourhood(store)
        link_id = _machine_link(store, a, close[0])
        (hit,) = cand.redundant_link_candidates(store)
        assert hit.detector == "redundant_link" and hit.action == "prune"
        assert hit.target_kind == "link" and hit.target_id == link_id
        assert hit.key == f"prune:link:{link_id}:0"
        assert hit.payload == {
            "link_id": link_id,
            "a": a,
            "b": close[0],
            "distance": hit.payload["distance"],
        }
        assert hit.payload["distance"] < cand.PRUNE_MAX_DISTANCE

    def test_non_mutual_and_human_pairs_not_proposed(self, store: Store) -> None:
        a, close, b = self._neighbourhood(store)
        # a–b is inside the distance floor but b is only a's sixth
        # nearest — the mutual top-5 leg fails.
        _machine_link(store, a, b)
        # n2–n3 are mutual top-5 (dist ~0.087) but the edge is a plain
        # test write — trust_origin 'unknown', never a machine candidate.
        store.add_link(src_ref_id=close[1], dst_ref_id=close[2], relation="related-to")
        assert cand.redundant_link_candidates(store) == []

    def test_scan_cap_bounds_the_scan(self, store: Store) -> None:
        def _pair(tag: str) -> tuple[int, int]:
            x = _ref(store, "concept", f"x {tag}")
            y = _ref(store, "concept", f"y {tag}")
            _card(store, x, f"shared card {tag}")
            _card(store, y, f"shared card {tag}")
            return x, y

        first = _machine_link(store, *_pair("one"))
        _machine_link(store, *_pair("two"))
        hits = cand.redundant_link_candidates(store, scan_cap=1)
        assert [h.target_id for h in hits] == [first]

    def test_key_dedups_across_two_ticks(self, store: Store) -> None:
        a, close, _b = self._neighbourhood(store)
        _machine_link(store, a, close[0])
        out = run_garden_tick(store, passes=("prune",), propose=_accept)
        assert out.proposals == 1
        out2 = run_garden_tick(store, passes=("prune",), propose=_accept)
        assert out2.proposals == 0 and out2.skipped_pending == 1
        (prop,) = pending_proposals(store, action="prune")
        assert prop.payload["a"] == a and prop.payload["b"] == close[0]


# ── reviewer burn-down: sampled non-terminal verdicts, agreement stats ─────


def _proposal(store: Store, rid: int, *, detector: str = "stale") -> int:
    rid_out = emit_proposal(
        store,
        GardenCandidate(
            detector=detector,
            action="prune",
            target_kind="ref",
            target_id=rid,
            title=f"{detector} on {rid}",
        ),
        rationale="seeded",
        model="test-model",
    )
    assert rid_out is not None
    return int(rid_out)


def _age(store: Store, review_id: int, days: int) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE reviews SET at = at - make_interval(days => %s) "
            "WHERE review_id = %s",
            (days, review_id),
        )


def _review_notes(store: Store, actor: str = REVIEWER_ACTOR) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        return [
            json.loads(r[0])["garden_review"]
            for r in conn.execute(
                "SELECT note FROM reviews WHERE actor = %s ORDER BY review_id",
                (actor,),
            ).fetchall()
        ]


def _fake(verdicts: dict[str, str]) -> Any:
    def call(_s: Store, p: Any) -> ReviewerVerdict:
        return ReviewerVerdict(
            verdict=verdicts.get(p.detector, "approve"),
            why=f"why on {p.detector}",
            model="fake-frontier",
        )

    return call


class TestReviewer:
    def test_age_gate_and_per_day_cap_oldest_first(self, store: Store) -> None:
        rids = [_ref(store, "concept", f"t{i}") for i in range(4)]
        p_new = _proposal(store, rids[0])
        p1 = _proposal(store, rids[1])
        p2 = _proposal(store, rids[2])
        p3 = _proposal(store, rids[3])
        _age(store, p1, 4)  # > min_age_days
        _age(store, p2, 6)
        _age(store, p3, 5)
        rep = review_sample(store, model_call=_fake({}), per_day=2, fable_per_day=0)
        assert rep.sampled == 2 and rep.errors == 0
        # Oldest two of the eligible three; the fresh proposal is untouched.
        assert {n["proposal"] for n in _review_notes(store)} == {p2, p3}
        assert p_new not in {n["proposal"] for n in _review_notes(store)}

    def test_payload_is_nonterminal_and_proposal_stays_open(self, store: Store) -> None:
        rid = _ref(store, "concept", "a stale node")
        pid = _proposal(store, rid, detector="stale")
        _age(store, pid, 4)
        cand_obj = GardenCandidate(
            detector="stale",
            action="prune",
            target_kind="ref",
            target_id=rid,
            title="t",
        )
        rep = review_sample(
            store, model_call=_fake({"stale": "reject"}), fable_per_day=0
        )
        assert rep.sampled == 1 and rep.rejections == 1
        (note,) = _review_notes(store)
        assert note["verdict"] == "reject" and note["sampled"] is True
        assert note["reviewed_by_model"] == "fake-frontier"
        assert note["why"].startswith("why") and note["detector"] == "stale"
        # Non-terminal: the proposal is still pending and its key held.
        assert proposal_key_pending(store, cand_obj)
        (prop,) = pending_proposals(store)
        assert prop.review_id == pid
        # And the reviewer row did not echo the garden key under note.garden.
        for rev in store.reviews_for("ref", rid):
            if rev.actor == REVIEWER_ACTOR:
                assert rev.verdict == "rejected"
                assert '"garden_review"' in (rev.note or "")
                assert '"garden"' not in (rev.note or "")

    def test_already_sampled_and_resolved_are_not_revisited(self, store: Store) -> None:
        from precis.graph.garden.proposals import resolve_proposal

        r1, r2 = _ref(store, "concept", "n1"), _ref(store, "concept", "n2")
        p1 = _proposal(store, r1)
        p2 = _proposal(store, r2)
        _age(store, p1, 4)
        _age(store, p2, 4)
        assert review_sample(store, model_call=_fake({}), fable_per_day=0).sampled == 2
        # A second run re-samples nothing — one review per proposal.
        assert review_sample(store, model_call=_fake({}), fable_per_day=0).sampled == 0
        resolve_proposal(store, p1, actor="reto", verdict="rejected")
        assert review_sample(store, model_call=_fake({}), fable_per_day=0).sampled == 0

    def test_agreement_stats_per_detector(self, store: Store) -> None:
        p_stale = _proposal(store, _ref(store, "concept", "s"), detector="stale")
        p_dup = _proposal(store, _ref(store, "concept", "d"), detector="dup_pair")
        p_dup2 = _proposal(store, _ref(store, "concept", "d2"), detector="dup_pair")
        for pid in (p_stale, p_dup, p_dup2):
            _age(store, pid, 4)
        rep = review_sample(
            store,
            model_call=_fake({"dup_pair": "reject", "stale": "approve"}),
            fable_per_day=0,
        )
        assert rep.sampled == 3 and rep.approvals == 1
        stats = agreement_stats(store)
        assert stats["stale"]["sampled"] == 1
        assert stats["stale"]["approvals"] == 1
        assert stats["stale"]["rate"] == 1.0
        assert stats["dup_pair"]["sampled"] == 2
        assert stats["dup_pair"]["approvals"] == 0
        assert stats["dup_pair"]["rate"] == 0.0

    def test_fable_rereviews_a_sample_and_stats_pair_it(self, store: Store) -> None:
        pids = [_proposal(store, _ref(store, "concept", f"n{i}")) for i in range(3)]
        for pid in pids:
            _age(store, pid, 4)

        def _fable(_s: Store, p: Any) -> ReviewerVerdict:
            # Fable agrees with Opus on the first proposal it sees,
            # disagrees on the second (Opus approves everything).
            verdict = "approve" if p.review_id == pids[0] else "reject"
            return ReviewerVerdict(verdict=verdict, why="fable why", model="fake-fable")

        rep = review_sample(
            store,
            model_call=_fake({}),
            fable_call=_fable,
            fable_per_day=2,
        )
        assert rep.sampled == 3 and rep.fable_sampled == 2
        notes = _review_notes(store, actor=REVIEWER_ACTOR_FABLE)
        assert {n["proposal"] for n in notes} == {pids[0], pids[1]}
        assert all(n["reviewed_by_model"] == "fake-fable" for n in notes)
        assert all(n["sampled"] is True for n in notes)
        # Same non-terminal shape: proposals still pending.
        assert len(pending_proposals(store)) == 3
        stats = agreement_stats(store)
        assert stats["stale"]["fable_sampled"] == 2
        assert stats["stale"]["fable_agreements"] == 1
        assert stats["stale"]["fable_rate"] == 0.5
        # A second run still samples nothing — a Fable row does not
        # suppress the Opus slot, and the Opus row already landed.
        assert review_sample(store, model_call=_fake({}), fable_per_day=0).sampled == 0

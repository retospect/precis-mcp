"""The graph gardener's proposal consumer (gr506044) and the
short-sibling reject rule (gr504843).

DB-backed (``store`` fixture), never prod. The Opus verdict is the
``review=`` seam; the three prod false positives (reviews 8288
Pedal/Wheel, 8300 Height/Width, 8302 Hose/Pipe) are rebuilt as fixtures
with the definitions they carry on prod.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis.embedder import MockEmbedder
from precis.graph.garden import consumer
from precis.graph.garden.apply import undo_merge
from precis.graph.garden.candidates import SIBLING_REJECT_REASON, GardenCandidate
from precis.graph.garden.consumer import (
    CONSUMER_ACTOR,
    ConsumerVerdict,
    consume_proposals,
    reject_reasons,
)
from precis.graph.garden.proposals import (
    emit_proposal,
    key_block,
    pending_proposals,
)
from precis.graph.garden.proposer import Verdict
from precis.graph.garden.runner import run_garden_tick
from precis.store import Store

_EMB = MockEmbedder(dim=1024)
_EMBEDDER = "bge-m3"

#: (review id, winner, loser, winner definition, loser definition) as on
#: prod 2026-10-23.
_FALSE_POSITIVES = [
    (
        8288,
        "Pedal",
        "Wheel",
        "Pedal: a component category, carried over from the legacy "
        "component_categories registry, which recorded no fuller definition.",
        "Wheel: a component category, carried over from the legacy "
        "component_categories registry, which recorded no fuller definition.",
    ),
    (
        8300,
        "Height",
        "Width",
        "Height of the part's bounding extent across its section, perpendicular "
        "to width.",
        "Width of the part's bounding extent across its section (rectangular parts).",
    ),
    (
        8302,
        "Hose",
        "Pipe",
        "Flexible fluid/gas conveyance.",
        "Rigid fluid/gas conveyance.",
    ),
]


def _ref(
    store: Store, kind: str, title: str, meta: dict[str, Any] | None = None
) -> int:
    return int(
        store.insert_ref(kind=kind, slug=None, title=title, meta=dict(meta or {})).id
    )


def _taxon(store: Store, name: str, definition: str, parent: int) -> int:
    rid = _ref(store, "taxon", name, meta={"name": name, "definition": definition})
    store.add_link(src_ref_id=rid, dst_ref_id=parent, relation="specialises")
    return rid


def _sha(store: Store, ref_id: int) -> str:
    with store.pool.connection() as conn:
        row = conn.execute("SELECT precis_target_sha('ref', %s)", (ref_id,)).fetchone()
    assert row is not None and row[0] is not None
    return str(row[0])


def _card_vec(store: Store, ref_id: int, text: str) -> None:
    """Give a ref an embedded ``card_combined`` chunk (the dup leg's vector)."""
    cid = store.chunks.upsert_card_combined(ref_id, text)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, "
            "status, attempts) VALUES (%s, %s, %s, 'ok', 1)",
            (cid, _EMBEDDER, _EMB.embed_one(text)),
        )


def _merge_proposal(
    store: Store, winner: int, loser: int, kind: str = "taxon", **evidence: Any
) -> int:
    rid = emit_proposal(
        store,
        GardenCandidate(
            detector="dup_pair",
            action="merge",
            target_kind="ref",
            target_id=loser,
            other_id=winner,
            title=f"dup pair {kind} {winner}↔{loser}",
            evidence={"kind": kind, **evidence},
            payload={
                "loser": loser,
                "winner": winner,
                "kind": kind,
                "shas": {
                    str(loser): _sha(store, loser),
                    str(winner): _sha(store, winner),
                },
            },
        ),
        rationale="High lexical overlap (0.833) and low distance (0.1686)",
        model="castor",
    )
    assert rid is not None
    return rid


def _note(store: Store, review_id: int) -> dict[str, Any]:
    """The garden payload of the row that resolved ``review_id``."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT actor, verdict, note FROM reviews "
            "WHERE note LIKE %s ORDER BY review_id DESC LIMIT 1",
            (f'%"resolves": {review_id}, %',),
        ).fetchone()
    assert row is not None, f"proposal {review_id} was not resolved"
    return {"actor": row[0], "verdict": row[1], **json.loads(row[2])["garden"]}


def _live(store: Store, ref_id: int) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT retired_at IS NULL FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return bool(row and row[0])


class _Judge:
    """A review seam that answers ``verdict`` and records what it saw."""

    def __init__(self, verdict: str = "approve", reason: str = "") -> None:
        self.verdict, self.reason = verdict, reason
        self.seen: list[int] = []

    def __call__(self, _store: Store, prop: Any) -> ConsumerVerdict:
        self.seen.append(prop.review_id)
        return ConsumerVerdict(
            verdict=self.verdict, reason=self.reason, why="judged", model="opus"
        )


# ── the three prod false positives ─────────────────────────────────────────


class TestShortSiblingFalsePositives:
    def _seed(self, store: Store) -> list[tuple[int, int, int]]:
        parent = _ref(store, "taxon", "subject", meta={"name": "subject"})
        out = []
        for _rid, wname, lname, wdef, ldef in _FALSE_POSITIVES:
            w = _taxon(store, wname, wdef, parent)
            lo = _taxon(store, lname, ldef, parent)
            out.append((_merge_proposal(store, w, lo, distance=0.1686), w, lo))
        return out

    def test_consumer_rejects_all_three_without_a_model_call(
        self, store: Store
    ) -> None:
        """Even a judge that would approve never sees them: the rule
        decides, the merge is not applied, and the reason is the code."""
        seeded = self._seed(store)
        judge = _Judge("approve")
        report = consume_proposals(store, review=judge)
        assert judge.seen == []
        assert (report.applied, report.rejected) == (0, 3)
        assert report.reasons == {SIBLING_REJECT_REASON: 3}
        for review_id, w, lo in seeded:
            g = _note(store, review_id)
            assert (g["verdict"], g["actor"]) == ("rejected", CONSUMER_ACTOR)
            assert g["reason"] == SIBLING_REJECT_REASON
            assert _live(store, w) and _live(store, lo)
            assert key_block(store, "ref", lo, f"merge:ref:{lo}:{w}") == "rejected"
        assert pending_proposals(store) == []
        assert reject_reasons(store) == {"dup_pair": {SIBLING_REJECT_REASON: 3}}

    def test_tick_rejects_the_pattern_before_the_proposer(self, store: Store) -> None:
        """The detector still finds Height/Width (cards embed close); the
        runner rejects it by rule, records the reason, and never asks."""
        parent = _ref(store, "taxon", "subject", meta={"name": "subject"})
        for name, definition in (("Height", "Height of a part."), ("Width", "")):
            rid = _taxon(store, name, definition, parent)
            cid = store.chunks.upsert_card_combined(rid, name)
            with store.pool.connection() as conn:
                conn.execute(
                    "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, "
                    "status, attempts) VALUES (%s, %s, %s, 'ok', 1)",
                    (cid, _EMBEDDER, _EMB.embed_one("bounding extent of a part")),
                )
        asked: list[str] = []

        def _accept(_s: Store, c: GardenCandidate) -> Verdict:
            asked.append(c.key)
            return Verdict(accept=True, rationale="looks alike")

        out = run_garden_tick(
            store, passes=("merge",), kinds=("taxon",), propose=_accept
        )
        assert (out.candidates, out.rule_rejected, out.proposals) == (1, 1, 0)
        assert asked == []
        assert reject_reasons(store) == {"dup_pair": {SIBLING_REJECT_REASON: 1}}
        again = run_garden_tick(
            store, passes=("merge",), kinds=("taxon",), propose=_accept
        )
        assert (again.rule_rejected, again.skipped_rejected) == (0, 1)

    def test_names_sharing_a_word_or_long_names_still_go_to_the_judge(
        self, store: Store
    ) -> None:
        """Bore/Inner diameter share a word; "Price per gram" / "Cost per
        unit mass" (an approved prod merge) is too long for the rule."""
        parent = _ref(store, "taxon", "measurand", meta={"name": "measurand"})
        pairs = [
            ("Inner diameter", "Bore diameter"),
            ("Cost per unit mass", "Price per gram"),
        ]
        for wname, lname in pairs:
            w = _taxon(store, wname, f"{wname}.", parent)
            lo = _taxon(store, lname, f"{lname}.", parent)
            _merge_proposal(store, w, lo)
        judge = _Judge("reject", "specialisation")
        report = consume_proposals(store, review=judge)
        assert len(judge.seen) == 2
        assert report.reasons == {"specialisation": 2}


# ── part 2: the Opus review ────────────────────────────────────────────────


class TestOpusReview:
    def test_approve_applies_an_undoable_merge(self, store: Store) -> None:
        a = _ref(store, "concept", "band gap")
        b = _ref(store, "concept", "band-gap")
        # The pair qualifies on the embedding leg (names differ
        # lexically) — the apply-time floor re-check needs it.
        for rid in (a, b):
            _card_vec(store, rid, "band gap")
        other = _ref(store, "concept", "semiconductor")
        store.add_link(src_ref_id=other, dst_ref_id=b, relation="related-to")
        review_id = _merge_proposal(store, a, b, kind="concept")
        report = consume_proposals(store, review=_Judge("approve"))
        assert report.applied == 1 and len(report.merges) == 1
        g = _note(store, review_id)
        assert g["verdict"] == "approved" and g["applied"] == report.merges[0]
        assert not _live(store, b)
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta FROM refs WHERE ref_id = %s", (b,)
            ).fetchone()
        assert row is not None and row[0]["superseded_by"] == a  # the redirect
        undo_merge(store, report.merges[0])
        assert _live(store, b)

    def test_reject_carries_a_reason_code(self, store: Store) -> None:
        a = _ref(store, "concept", "unit cost")
        b = _ref(store, "concept", "cost per unit mass")
        review_id = _merge_proposal(store, a, b, kind="concept")
        consume_proposals(store, review=_Judge("reject", "specialisation"))
        g = _note(store, review_id)
        assert (g["verdict"], g["reason"], g["by_model"]) == (
            "rejected",
            "specialisation",
            "opus",
        )
        assert _live(store, a) and _live(store, b)

    def test_unknown_reason_is_recorded_as_other(self, store: Store) -> None:
        a = _ref(store, "concept", "alpha phase")
        b = _ref(store, "concept", "beta phase")
        review_id = _merge_proposal(store, a, b, kind="concept")
        consume_proposals(store, review=_Judge("reject", "vibes"))
        assert _note(store, review_id)["reason"] == "other"

    def test_review_limit_and_pause(self, store: Store) -> None:
        for i in range(3):
            _merge_proposal(
                store,
                _ref(store, "concept", f"x term {i}"),
                _ref(store, "concept", f"y term {i}"),
                kind="concept",
            )
        judge = _Judge("reject", "distinct_terms")
        assert consume_proposals(store, review=judge, review_limit=2).rejected == 2

        def _paused(_s: Store, _p: Any) -> ConsumerVerdict:
            return ConsumerVerdict(verdict="", paused=True, error="quota")

        report = consume_proposals(store, review=_paused)
        assert report.paused and report.rejected == 0
        assert len(pending_proposals(store)) == 1


# ── part 1: auto-prune ─────────────────────────────────────────────────────


def _dangling(store: Store, *, set_by: str = "agent") -> tuple[int, int, int]:
    from precis.graph.garden.candidates import dangling_edge_candidates

    live = _ref(store, "memory", "live note")
    dead = _ref(store, "concept", "dead thing")
    link = int(
        store.add_link(
            src_ref_id=live, dst_ref_id=dead, relation="related-to", set_by=set_by
        ).id
    )
    store.retire_ref(dead)
    (c,) = dangling_edge_candidates(store)
    rid = emit_proposal(store, c, rationale="dead end")
    assert rid is not None
    return rid, link, dead


def _link_exists(store: Store, link_id: int) -> bool:
    with store.pool.connection() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM links WHERE link_id = %s", (link_id,)
            ).fetchone()
            is not None
        )


class TestAutoPrune:
    def test_dangling_edge_is_pruned_with_a_snapshot(self, store: Store) -> None:
        review_id, link, _dead = _dangling(store)
        report = consume_proposals(store, review=_Judge())
        assert report.pruned == 1 and not _link_exists(store, link)
        g = _note(store, review_id)
        assert (g["verdict"], g["actor"], g["applied"]) == (
            "approved",
            "graph-garden",
            "prune",
        )
        assert g["link"]["link_id"] == link and g["link"]["relation"] == "related-to"

    def test_human_edge_closes_as_human_edge(self, store: Store) -> None:
        """A human's edge is never touched — and the proposal closes
        instead of filling the pending window every tick."""
        review_id, link, _dead = _dangling(store, set_by="user")
        report = consume_proposals(store, review=_Judge())
        assert report.pruned == 0 and _link_exists(store, link)
        g = _note(store, review_id)
        assert (g["verdict"], g["reason"]) == ("rejected", "human_edge")
        assert pending_proposals(store) == []

    def test_restored_endpoint_closes_as_not_dangling(self, store: Store) -> None:
        review_id, link, dead = _dangling(store)
        store.restore_ref(dead)
        consume_proposals(store, review=_Judge())
        assert _link_exists(store, link)
        g = _note(store, review_id)
        assert (g["verdict"], g["reason"]) == ("rejected", "not_dangling")


# ── the job runs it after every tick ───────────────────────────────────────


class _Ctx:
    def __init__(self, store: Any, params: dict[str, Any]) -> None:
        self.store = store
        self.meta: dict[str, Any] = {"params": params}
        self.events: list[tuple[str, str]] = []
        self.failure: str | None = None

    def append_chunk(self, kind: str, text: str) -> None:
        self.events.append((kind, text))

    def set_meta(self, **fields: Any) -> None:
        self.meta.update(fields)

    def record_failure(self, reason: str, **_kw: Any) -> None:
        self.failure = reason

    def defer(self, _delay: Any, _reason: str) -> bool:
        return True


class TestJobStep:
    def _lit(self, store: Store) -> None:
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO service_config (host, service, prio) "
                "VALUES ('test-host', 'graph_garden', 1) "
                "ON CONFLICT (host, service) DO UPDATE SET prio = 1"
            )

    def test_dispatch_consumes_after_the_tick(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.workers.job_types.graph_garden import SPEC, _dispatch

        self._lit(store)
        _dangling(store)
        review_id = _merge_proposal(
            store,
            _ref(store, "concept", "alpha phase"),
            _ref(store, "concept", "beta phase"),
            kind="concept",
        )
        monkeypatch.setattr(consumer, "consume_llm", _Judge("reject", "distinct_terms"))
        ctx = _Ctx(store, {"pass": "split"})
        _dispatch(ctx, SPEC)
        done = ctx.meta["garden"]["consumed"]
        assert (done["pruned"], done["rejected"]) == (1, 1)
        assert _note(store, review_id)["reason"] == "distinct_terms"
        assert any("consumer" in text for _k, text in ctx.events)

    def test_consume_false_skips_it(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.workers.job_types.graph_garden import SPEC, _dispatch

        self._lit(store)
        _dangling(store)
        ctx = _Ctx(store, {"pass": "split", "consume": False})
        _dispatch(ctx, SPEC)
        assert "consumed" not in ctx.meta["garden"]
        assert len(pending_proposals(store)) == 1

    def test_consume_failure_is_meta_not_a_tick_failure(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A consumer exception lands on meta.garden.consume_error; the
        tick's own proposals and outcome are untouched."""
        from precis.workers.job_types.graph_garden import SPEC, _dispatch

        self._lit(store)
        _dangling(store)

        def _boom(*_a: Any, **_kw: Any) -> Any:
            raise RuntimeError("reviewer blew up")

        monkeypatch.setattr(consumer, "consume_proposals", _boom)
        ctx = _Ctx(store, {"pass": "split"})
        _dispatch(ctx, SPEC)
        assert ctx.meta["garden"]["consume_error"].startswith(
            "RuntimeError: reviewer blew up"
        )
        assert ctx.failure is None
        assert len(pending_proposals(store)) == 1  # the edge stays proposed


# ── review-time guards ─────────────────────────────────────────────────────


def _prune_proposal(store: Store, link_id: int, other: int) -> int:
    """File a prune proposal directly — the shape a pre-narrowing tick
    wrote, before the detector excluded history/provenance edges."""
    rid = emit_proposal(
        store,
        GardenCandidate(
            detector="dangling_edge",
            action="prune",
            target_kind="link",
            target_id=link_id,
            other_id=other,
            title=f"edge {link_id} touches a retired ref",
            evidence={},
            payload={"delete_link": link_id},
        ),
        rationale="filed before the dangling_edge narrowing",
    )
    assert rid is not None
    return rid


class TestPruneRecheck:
    """_prune_one re-runs the narrowed dangling predicate at apply time."""

    def test_done_todo_endpoint_is_history_not_dangling(self, store: Store) -> None:
        live = _ref(store, "concept", "live concept")
        done = _ref(store, "todo", "finished task")
        link = int(
            store.add_link(src_ref_id=live, dst_ref_id=done, relation="related-to").id
        )
        store.retire_ref(done)
        review_id = _prune_proposal(store, link, done)
        report = consume_proposals(store, review=_Judge())
        assert report.pruned == 0 and _link_exists(store, link)
        g = _note(store, review_id)
        assert (g["verdict"], g["reason"]) == ("rejected", "not_dangling")

    def test_both_endpoints_retired_is_not_dangling(self, store: Store) -> None:
        a = _ref(store, "concept", "dead a")
        b = _ref(store, "concept", "dead b")
        link = int(store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to").id)
        store.retire_ref(a)
        store.retire_ref(b)
        review_id = _prune_proposal(store, link, b)
        report = consume_proposals(store, review=_Judge())
        assert report.pruned == 0 and _link_exists(store, link)
        assert _note(store, review_id)["reason"] == "not_dangling"

    def test_decided_concurrently_skips_the_delete(self, store: Store) -> None:
        """resolve_proposal returning None means another decider won —
        the link must not be deleted on a lost race."""
        from precis.graph.garden.consumer import ConsumeReport, _prune_one
        from precis.graph.garden.proposals import resolve_proposal

        review_id, link, _dead = _dangling(store)
        prop = next(
            p
            for p in pending_proposals(store, action="prune")
            if p.review_id == review_id
        )
        resolve_proposal(store, review_id, actor="user", verdict="rejected")
        _prune_one(store, prop, ConsumeReport())
        assert _link_exists(store, link)


class TestReviewBounds:
    def test_errors_bound_attempts_and_stop_after_three(self, store: Store) -> None:
        """A reviewer whose verdict never parses counts as attempts and
        stops the tick after MAX_CONSECUTIVE_ERRORS."""
        for i in range(5):
            _merge_proposal(
                store,
                _ref(store, "concept", f"alpha {i}"),
                _ref(store, "concept", f"beta {i}"),
                kind="concept",
            )
        calls: list[int] = []

        def _broken(_s: Store, p: Any) -> ConsumerVerdict:
            calls.append(p.review_id)
            return ConsumerVerdict(verdict="", error="unusable verdict 'x'")

        report = consume_proposals(store, review=_broken, review_limit=10)
        assert len(calls) == consumer.MAX_CONSECUTIVE_ERRORS == 3
        assert (report.attempts, report.errors, report.reviewed) == (3, 3, 0)
        assert len(pending_proposals(store)) == 5

    def test_attempt_bound_counts_errors(self, store: Store) -> None:
        """Below the consecutive cap, the limit still counts attempts —
        4 pending proposals, limit 2, all failing: exactly 2 calls."""
        for i in range(4):
            _merge_proposal(
                store,
                _ref(store, "concept", f"gamma {i}"),
                _ref(store, "concept", f"delta {i}"),
                kind="concept",
            )
        calls: list[int] = []

        def _broken(_s: Store, p: Any) -> ConsumerVerdict:
            calls.append(p.review_id)
            return ConsumerVerdict(verdict="", error="boom")

        consume_proposals(store, review=_broken, review_limit=2)
        assert len(calls) == 2


class TestReviewFilters:
    def test_sibling_confusion_never_reaches_the_reviewer(self, store: Store) -> None:
        """A merge-shaped proposal with siblings/options and no
        loser/winner is a decision for a human — not an apply, not an
        apply_refused reject."""
        parent = _ref(store, "taxon", "axis", meta={"name": "axis"})
        a = _taxon(store, "confused a", "def a", parent)
        b = _taxon(store, "confused b", "def b", parent)
        rid = emit_proposal(
            store,
            GardenCandidate(
                detector="sibling_confusion",
                action="merge",
                target_kind="ref",
                target_id=b,
                other_id=a,
                title="sibling confusion",
                evidence={"axis": "phase"},
                payload={
                    "axis": "phase",
                    "siblings": [a, b],
                    "options": ["merge", "sharpen", "re-axis", "re-parent"],
                },
            ),
            rationale="shared instances on an exclusive axis",
        )
        assert rid is not None
        judge = _Judge("approve")
        report = consume_proposals(store, review=judge)
        assert judge.seen == []
        assert report.rejected == 0 and report.applied == 0
        assert [p.review_id for p in pending_proposals(store)] == [rid]

    def test_approved_relink_is_recorded_once_then_filtered(self, store: Store) -> None:
        """An approved relink stays pending (gr507641, no apply door)
        but is never re-reviewed: the SQL filter drops it before the
        reviewer is consulted."""
        orphan = _ref(store, "taxon", "orphan", meta={"name": "orphan"})
        parent = _ref(store, "taxon", "parent", meta={"name": "parent"})
        rid = emit_proposal(
            store,
            GardenCandidate(
                detector="orphan",
                action="relink",
                target_kind="ref",
                target_id=orphan,
                other_id=parent,
                title="orphan relink",
                evidence={},
                payload={"child": orphan, "parent": parent},
            ),
            rationale="orphan taxon, nearest start node",
        )
        assert rid is not None
        judge = _Judge("approve")
        first = consume_proposals(store, review=judge)
        assert judge.seen == [rid] and first.approved_unapplied == 1
        second = consume_proposals(store, review=judge)
        assert second.attempts == 0 and len(judge.seen) == 1
        # And the proposal is still pending — for a human, per the ruling gap.
        assert [p.review_id for p in pending_proposals(store)] == [rid]


class TestApplyRecheck:
    def test_winner_drift_closes_as_stale(self, store: Store) -> None:
        """The merge was approved on the winner as pinned; an edit to the
        winner after the proposal must not ride the stale verdict."""
        a = _ref(store, "concept", "band gap")
        b = _ref(store, "concept", "bandgap")
        for rid_ in (a, b):
            _card_vec(store, rid_, "band gap")
        review_id = _merge_proposal(store, a, b, kind="concept")
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET title = 'band gap (SI)' WHERE ref_id = %s",
                (a,),
            )
        report = consume_proposals(store, review=_Judge("approve"))
        assert (report.applied, report.rejected) == (0, 1)
        g = _note(store, review_id)
        assert (g["verdict"], g["reason"]) == ("rejected", "stale")
        assert _live(store, a) and _live(store, b)

    def test_pair_below_the_floor_is_apply_refused(self, store: Store) -> None:
        """No shared term and no sub-floor embedding: even an approve
        cannot merge a pair the detector would not propose today."""
        a = _ref(store, "concept", "electrolyte")
        b = _ref(store, "concept", "gear ratio")
        review_id = _merge_proposal(store, a, b, kind="concept")
        report = consume_proposals(store, review=_Judge("approve"))
        assert (report.applied, report.rejected) == (0, 1)
        g = _note(store, review_id)
        assert (g["verdict"], g["reason"]) == ("rejected", "apply_refused")
        assert "no shared term" in g["why"]
        assert _live(store, a) and _live(store, b)

    def test_second_tick_is_a_noop(self, store: Store) -> None:
        """Everything the first consume decided stays decided."""
        _dangling(store)
        a = _ref(store, "concept", "band gap")
        b = _ref(store, "concept", "bandgap")
        for rid_ in (a, b):
            _card_vec(store, rid_, "band gap")
        _merge_proposal(store, a, b, kind="concept")
        first = consume_proposals(store, review=_Judge("approve"))
        assert (first.pruned, first.applied) == (1, 1)
        second = consume_proposals(store, review=_Judge("approve"))
        assert (
            second.pruned,
            second.prune_closed,
            second.attempts,
            second.reviewed,
            second.rejected,
            second.errors,
        ) == (0, 0, 0, 0, 0, 0)

    def test_undo_of_a_consumer_applied_merge(self, store: Store) -> None:
        """The full round trip through the consumer: approve → applied
        → undo restores the loser, its edges and the winner's aliases."""
        a = _ref(store, "concept", "band gap")
        b = _ref(store, "concept", "bandgap")
        for rid_ in (a, b):
            _card_vec(store, rid_, "band gap")
        m = _ref(store, "memory", "a note naming the loser")
        store.add_link(src_ref_id=m, dst_ref_id=b, relation="related-to")
        _merge_proposal(store, a, b, kind="concept")
        report = consume_proposals(store, review=_Judge("approve"))
        assert report.applied == 1
        assert not _live(store, b)
        with store.pool.connection() as conn:
            edge = conn.execute(
                "SELECT dst_ref_id FROM links WHERE src_ref_id = %s", (m,)
            ).fetchone()
        assert edge is not None and edge[0] == a  # repointed
        counts = undo_merge(store, report.merges[0])
        assert (counts["links_restored"], counts["links_deleted"]) == (1, 1)
        assert _live(store, b)
        with store.pool.connection() as conn:
            edge = conn.execute(
                "SELECT dst_ref_id FROM links WHERE src_ref_id = %s", (m,)
            ).fetchone()
            assert edge is not None and edge[0] == b  # back to the loser
            meta = conn.execute(
                "SELECT meta FROM refs WHERE ref_id = %s", (b,)
            ).fetchone()
        assert meta is not None and "garden_merge" not in meta[0]

    def test_apply_door_refusal_closes_as_apply_refused(self, store: Store) -> None:
        """A merge the door itself refuses (cross-kind) lands as
        apply_refused even when the reviewer approved."""
        a = _ref(store, "concept", "left thing")
        b = _ref(store, "memory", "right thing")
        rid = emit_proposal(
            store,
            GardenCandidate(
                detector="other_pass",
                action="merge",
                target_kind="ref",
                target_id=b,
                other_id=a,
                title="cross-kind pair",
                evidence={},
                payload={
                    "loser": b,
                    "winner": a,
                    "kind": "concept",
                    "shas": {str(a): _sha(store, a), str(b): _sha(store, b)},
                },
            ),
            rationale="r",
        )
        assert rid is not None
        report = consume_proposals(store, review=_Judge("approve"))
        assert (report.applied, report.rejected) == (0, 1)
        g = _note(store, rid)
        assert (g["verdict"], g["reason"]) == ("rejected", "apply_refused")
        assert "same-kind" in g["why"]
        assert _live(store, a) and _live(store, b)

    def test_merge_without_sha_pins_closes_stale(self, store: Store) -> None:
        """Proposals filed before payload.shas existed cannot be
        re-verified — closed as stale without a model call."""
        a = _ref(store, "concept", "legacy one")
        b = _ref(store, "concept", "legacy two")
        rid = emit_proposal(
            store,
            GardenCandidate(
                detector="dup_pair",
                action="merge",
                target_kind="ref",
                target_id=b,
                other_id=a,
                title="legacy dup pair",
                evidence={},
                payload={"loser": b, "winner": a, "kind": "concept"},
            ),
            rationale="pre-pinning proposal",
        )
        assert rid is not None
        judge = _Judge("approve")
        report = consume_proposals(store, review=judge)
        assert judge.seen == []  # never reviewed
        assert (report.applied, report.rejected) == (0, 1)
        g = _note(store, rid)
        assert (g["verdict"], g["reason"]) == ("rejected", "stale")
        assert _live(store, a) and _live(store, b)

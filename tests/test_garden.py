"""Graph gardener — acceptance criteria 1-6 of the closed graph-gardener spec.

DB-backed (``store`` fixture): seeded pairs exercise the deterministic
detectors, the proposals-only ledger write, the explicit apply/undo door,
and the dark/cooled run gates. The LLM is never invoked — the runner's
``propose=`` seam takes a deterministic verdict, and the dispatch glue is
driven through a stub context with a monkeypatched proposer.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from psycopg.errors import QueryCanceled

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.errors import BadInput, NotFound
from precis.graph.garden import candidates as cand
from precis.graph.garden.apply import apply_merge, undo_merge
from precis.graph.garden.candidates import GardenCandidate
from precis.graph.garden.proposals import (
    emit_proposal,
    pending_proposals,
    resolve_proposal,
)
from precis.graph.garden.proposer import Verdict
from precis.graph.garden.runner import run_garden_tick
from precis.graph.garden.service import (
    DRY_RUNS_ESCALATE,
    cooling_active,
    graph_garden_enabled,
)
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.store.types import Tag

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


def _link(
    store: Store,
    src: int,
    dst: int,
    relation: str = "related-to",
    meta: dict[str, Any] | None = None,
) -> None:
    store.add_link(src_ref_id=src, dst_ref_id=dst, relation=relation, meta=meta)


def _accept(_store: Store, _cand: GardenCandidate) -> Verdict:
    return Verdict(accept=True, rationale="seeded accept", model="test-model")


def _links(store: Store) -> list[tuple[int, int, str]]:
    with store.pool.connection() as conn:
        return [
            (int(r[0]), int(r[1]), str(r[2]))
            for r in conn.execute(
                "SELECT src_ref_id, dst_ref_id, relation FROM links ORDER BY link_id"
            ).fetchall()
        ]


def _garden_rows(store: Store) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        return [
            json.loads(r[0])["garden"]
            for r in conn.execute(
                "SELECT note FROM reviews WHERE actor = 'graph-garden' "
                "ORDER BY review_id"
            ).fetchall()
        ]


def _table_count(store: Store, table: str) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    return int(row[0]) if row else 0


def _live(store: Store, ref_id: int) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT retired_at IS NULL FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return bool(row[0]) if row else False


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return dict(row[0] or {}) if row else {}


def _lit_service(store: Store) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO service_config (host, service, prio) "
            "VALUES ('test-host', 'graph_garden', 1) "
            "ON CONFLICT (host, service) DO UPDATE SET prio = 1"
        )


def _seed_garden_job(
    store: Store, *, proposals: int, status: str, skipped: str | None = None
) -> int:
    rid = _ref(
        store,
        "job",
        "garden job",
        meta={
            "job_type": "graph_garden",
            "executor": "job_inproc",
            "garden": {
                "proposals": proposals,
                **({"skipped": skipped} if skipped is not None else {}),
            },
        },
    )
    store.add_tag(rid, Tag.closed("STATUS", status), set_by="system")
    return rid


# ── AC 1: duplicate pair → one merge proposal; dimension clash → none ──────


class TestAC1DuplicateMerge:
    def test_same_name_same_dimension_yields_one_merge_proposal(
        self, store: Store
    ) -> None:
        a = _ref(store, "taxon", "band gap", meta={"name": "band gap"})
        b = _ref(
            store,
            "taxon",
            "band gap",
            meta={
                "name": "band gap",
                "dimension_kind": "si",
                "si_vector": "0,0,0,1,0,0,0",
            },
        )
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
                (
                    json.dumps(
                        {
                            "dimension_kind": "si",
                            "si_vector": "0,0,0,1,0,0,0",
                        }
                    ),
                    a,
                ),
            )
        out = run_garden_tick(store, passes=("merge",), propose=_accept)
        assert out.candidates == 1 and out.proposals == 1
        (row,) = _garden_rows(store)
        assert row["action"] == "merge" and row["detector"] == "dup_pair"
        payload = row["payload"]
        assert (payload["loser"], payload["winner"], payload["kind"]) == (
            b,
            a,
            "taxon",
        )
        # Both endpoints' shas pinned at proposal time (apply re-check).
        assert set(payload["shas"]) == {str(a), str(b)}
        with store.pool.connection() as conn:
            live: dict[int, str] = {
                int(k): str(v)
                for k, v in conn.execute(
                    "SELECT ref_id, precis_target_sha('ref', ref_id) "
                    "FROM refs WHERE ref_id = ANY(%s)",
                    ([a, b],),
                ).fetchall()
            }
        assert payload["shas"] == {str(k): v for k, v in live.items()}

    def test_si_vector_mismatch_yields_nothing_even_with_identical_embeddings(
        self, store: Store
    ) -> None:
        a = _ref(
            store,
            "taxon",
            "band gap",
            meta={
                "name": "band gap",
                "dimension_kind": "si",
                "si_vector": "0,0,0,1,0,0,0",
            },
        )
        b = _ref(
            store,
            "taxon",
            "band gap",
            meta={
                "name": "band gap",
                "dimension_kind": "si",
                "si_vector": "0,0,-1,0,0,0,0",
            },
        )
        _card(store, a, "band gap — identical card text")
        _card(store, b, "band gap — identical card text")
        out = run_garden_tick(store, passes=("merge",), propose=_accept)
        assert out.candidates == 0 and out.proposals == 0
        assert _garden_rows(store) == []

    def test_embedding_leg_flags_dup_without_a_shared_name(self, store: Store) -> None:
        a = _ref(store, "concept", "reciprocal lattice", meta={})
        b = _ref(store, "concept", "dual space", meta={})
        _card(store, a, "the same crystallographic construction")
        _card(store, b, "the same crystallographic construction")
        hits = cand.duplicate_candidates(store, kinds=("concept",))
        assert [(c.target_id, c.other_id) for c in hits] == [(b, a)]
        assert hits[0].evidence["distance"] < cand.DUP_MAX_DISTANCE


# ── AC 2: polysemy finding → split proposal; none without ─────────────────


class TestAC2SplitPolysemy:
    def test_finding_yields_split_proposal_naming_uses(self, store: Store) -> None:
        node = _ref(store, "concept", "mercury", meta={})
        finding = _ref(
            store,
            "finding",
            "mercury is polysemous",
            meta={"annotation": "polysemy", "uses": ["the planet", "the metal"]},
        )
        _link(store, finding, node)
        out = run_garden_tick(store, passes=("split",), propose=_accept)
        assert out.proposals == 1
        (row,) = _garden_rows(store)
        assert row["action"] == "split" and row["detector"] == "polysemy"
        assert row["evidence"]["uses"] == ["the planet", "the metal"]
        assert row["evidence"]["hint"] is False

    def test_same_node_without_finding_yields_nothing(self, store: Store) -> None:
        _ref(store, "concept", "mercury", meta={})
        assert (
            run_garden_tick(store, passes=("split",), propose=_accept).candidates == 0
        )

    def test_disjoint_start_clusters_flag_a_split_hint(self, store: Store) -> None:
        """A taxon linked from disjoint sibling clusters (two different
        start hierarchies) is the split signal the 2026-10-05 ruling named —
        flagged as a hint, weaker than a finding."""
        s1 = _ref(store, "taxon", "root one", meta={"start": True})
        s2 = _ref(store, "taxon", "root two", meta={"start": True})
        mid = _ref(store, "taxon", "shared child", meta={})
        _link(store, mid, s1, "specialises")
        _link(store, mid, s2, "specialises")
        hits = cand.polysemy_candidates(store)
        assert len(hits) == 1
        assert hits[0].target_id == mid and hits[0].evidence["hint"] is True
        assert hits[0].evidence["start_nodes"] == sorted([s1, s2])


# ── AC 3: orphan taxon → relink proposal naming a candidate parent ─────────


class TestAC3RelinkOrphan:
    def test_orphan_yields_relink_naming_candidate_parent(self, store: Store) -> None:
        start = _ref(store, "taxon", "measurands", meta={"start": True})
        orphan = _ref(store, "taxon", "zorbance", meta={"name": "zorbance"})
        _card(store, start, "measurands — the quantities a campaign tracks")
        _card(store, orphan, "zorbance — quux per unit splort")
        out = run_garden_tick(store, passes=("relink",), propose=_accept)
        assert out.proposals == 1
        (row,) = _garden_rows(store)
        assert row["action"] == "relink" and row["detector"] == "orphan"
        assert row["payload"]["parent"] == start
        assert row["evidence"]["start_nodes"] == [start]

    def test_no_start_nodes_means_nothing_to_relink_toward(self, store: Store) -> None:
        _ref(store, "taxon", "zorbance", meta={"name": "zorbance"})
        assert cand.orphan_taxon_candidates(store) == []


# ── AC 4: applied merge repoints mentions, loser resolves as alias; undo ───


class TestAC4MergeApplyUndo:
    def _pair(self, store: Store) -> tuple[int, int, int, int]:
        winner = _ref(store, "taxon", "alpha term", meta={"name": "alpha term"})
        loser = _ref(
            store,
            "taxon",
            "alpha prime",
            meta={"name": "alpha prime", "norm_name": "alpha prime"},
        )
        mentioner = _ref(store, "memory", "a note about alpha prime")
        target = _ref(store, "concept", "supported claim")
        _link(store, mentioner, loser)  # mention → repoints to winner
        _link(store, loser, target, "supports")  # outbound → repoints
        _link(store, loser, winner)  # loser↔winner → dropped self-loop
        return winner, loser, mentioner, target

    def test_apply_repoints_mentions_loser_resolves_as_alias(
        self, store: Store
    ) -> None:
        winner, loser, mentioner, target = self._pair(store)
        proposal = emit_proposal(
            store,
            GardenCandidate(
                detector="dup_pair",
                action="merge",
                target_kind="ref",
                target_id=loser,
                other_id=winner,
                title="dup pair",
            ),
            rationale="same term",
            model="test-model",
        )
        assert proposal is not None
        rec = apply_merge(store, loser, winner, actor="reto", review_id=proposal)
        # Mentions repointed onto the winner, self-loop dropped.
        assert _links(store) == [
            (mentioner, winner, "related-to"),
            (winner, target, "supports"),
        ]
        assert not _live(store, loser)
        loser_meta = _meta(store, loser)
        assert loser_meta["superseded_by"] == winner
        assert loser_meta["garden_merge"]["merge_id"] == rec.merge_id
        assert "alpha prime" in (_meta(store, winner).get("aliases") or [])
        # The loser resolves as an alias of the winner (retired → excluded).
        found = store.taxon_find_by_term(slug="", norm="alpha prime")
        assert found == [winner]
        # Second bar recorded: the proposal is approved, not pending.
        verdicts = [(r.verdict, r.actor) for r in store.reviews_for("ref", loser)]
        assert verdicts[0] == ("approved", "reto")
        assert ("proposed", "graph-garden") in verdicts
        assert pending_proposals(store) == []

        counts = undo_merge(store, rec.merge_id)
        assert counts["links_restored"] == 3 and counts["links_deleted"] == 2
        # Both nodes live again and every repointed mention is back.
        assert _live(store, loser) and _live(store, winner)
        assert _links(store) == [
            (mentioner, loser, "related-to"),
            (loser, target, "supports"),
            (loser, winner, "related-to"),
        ]
        assert "superseded_by" not in _meta(store, loser)
        assert "garden_merge" not in _meta(store, loser)
        assert "alpha prime" not in (_meta(store, winner).get("aliases") or [])
        assert store.taxon_find_by_term(slug="", norm="alpha prime") == [loser]

    def test_apply_refusals(self, store: Store) -> None:
        winner, loser, _m, _t = self._pair(store)
        with pytest.raises(BadInput, match="distinct"):
            apply_merge(store, loser, loser, actor="reto")
        with pytest.raises(BadInput, match="same-kind"):
            apply_merge(store, loser, _m, actor="reto")
        with pytest.raises(NotFound):
            apply_merge(store, loser, winner, actor="reto", review_id=999999)
        apply_merge(store, loser, winner, actor="reto")
        with pytest.raises(BadInput, match="retired"):
            apply_merge(store, loser, winner, actor="reto")


# ── AC 5: clean graph → zero proposals, zero writes; cooling ──────────────


class TestAC5QuietCooling:
    def test_clean_graph_no_candidates_no_writes(self, store: Store) -> None:
        out = run_garden_tick(store, propose=_accept)
        assert out.candidates == 0 and out.proposals == 0
        assert _table_count(store, "reviews") == 0
        assert _table_count(store, "chunks") == 0
        assert _table_count(store, "links") == 0

    def test_enabled_gate(self, store: Store) -> None:
        assert graph_garden_enabled(store) is False
        _lit_service(store)
        assert graph_garden_enabled(store) is True

    def test_cooling_counts_runs_and_re_arms(self, store: Store) -> None:
        """Cooling is counted in runs, not days: a streak of
        ``escalate_after`` dry succeeded runs trips it, it holds for
        ``cool_runs`` gate-skipped runs, and it re-arms from the next
        real run (matching the retired day-counted cooldown)."""
        for _ in range(3):
            _seed_garden_job(store, proposals=0, status="succeeded")
        assert cooling_active(store, escalate_after=3, cool_runs=2) is True
        # Gate-skipped runs count the window down — one at a time.
        _seed_garden_job(store, proposals=0, status="succeeded", skipped="cooled")
        assert cooling_active(store, escalate_after=3, cool_runs=2) is True
        _seed_garden_job(store, proposals=0, status="succeeded", skipped="cooled")
        assert cooling_active(store, escalate_after=3, cool_runs=2) is False
        # A real dry run after the window re-arms the whole cycle.
        _seed_garden_job(store, proposals=0, status="succeeded")
        assert cooling_active(store, escalate_after=3, cool_runs=2) is True
        # A productive run breaks the streak → cooled off.
        _seed_garden_job(store, proposals=2, status="succeeded")
        assert cooling_active(store, escalate_after=3, cool_runs=2) is False

    def test_cooling_skips_inflight_and_resets_on_failure(self, store: Store) -> None:
        """An in-flight newest job is evidence neither way — the dispatch
        evaluates cooling while its own job sits queued/running. A failed
        run resets the streak (a broken pass is not a quiet pass)."""
        for _ in range(3):
            _seed_garden_job(store, proposals=0, status="succeeded")
        _seed_garden_job(store, proposals=0, status="queued")
        assert cooling_active(store, escalate_after=3, cool_runs=2) is True
        _seed_garden_job(store, proposals=0, status="failed")
        assert cooling_active(store, escalate_after=3, cool_runs=2) is False


# ── AC 6: exclusive-axis sibling label swap → one confusion proposal ───────


class TestAC6SiblingConfusion:
    def _siblings(self, store: Store) -> tuple[int, int]:
        parent = _ref(store, "taxon", "conditions", meta={})
        a = _ref(store, "taxon", "ambient", meta={})
        b = _ref(store, "taxon", "room temperature", meta={})
        _link(store, a, parent, "specialises", meta={"axis": "cond"})
        _link(store, b, parent, "specialises", meta={"axis": "cond"})
        return a, b

    def test_label_swap_yields_one_confusion_proposal(self, store: Store) -> None:
        a, b = self._siblings(store)
        inst = _ref(store, "concept", "a measured sample")
        _link(store, inst, a, "instance-of", meta={"run": "run-a"})
        _link(store, inst, b, "instance-of", meta={"run": "run-b"})
        out = run_garden_tick(
            store,
            passes=("siblings",),
            exclusive_axes=("cond",),
            propose=_accept,
        )
        assert out.proposals == 1
        (row,) = _garden_rows(store)
        assert row["detector"] == "sibling_confusion"
        ev = row["evidence"]
        assert ev["axis"] == "cond" and inst in ev["shared_instances"]
        assert ev["run_swaps"] == [
            {"instance": inst, "run_a": "run-a", "run_b": "run-b"}
        ]

    def test_clean_siblings_yield_nothing(self, store: Store) -> None:
        a, b = self._siblings(store)
        i1 = _ref(store, "concept", "sample one")
        i2 = _ref(store, "concept", "sample two")
        _link(store, i1, a, "instance-of", meta={"run": "run-a"})
        _link(store, i2, b, "instance-of", meta={"run": "run-a"})
        assert cand.sibling_confusion_candidates(store, exclusive_axes=("cond",)) == []

    def test_undeclared_axis_is_not_scanned(self, store: Store) -> None:
        a, b = self._siblings(store)
        inst = _ref(store, "concept", "a measured sample")
        _link(store, inst, a, "instance-of", meta={"run": "run-a"})
        _link(store, inst, b, "instance-of", meta={"run": "run-b"})
        assert cand.sibling_confusion_candidates(store, exclusive_axes=()) == []


# ── the proposals-only invariant + ledger dedup ────────────────────────────


class TestProposalsOnly:
    def test_tick_writes_only_reviews_rows(self, store: Store) -> None:
        # A dirty graph: a dup pair, a dangling edge, a stale node.
        a = _ref(store, "taxon", "dup term", meta={"name": "dup term"})
        b = _ref(store, "taxon", "dup term", meta={"name": "dup term"})
        dead = _ref(store, "concept", "retired thing")
        src = _ref(store, "memory", "points at the dead thing")
        _link(store, src, dead)
        store.retire_ref(dead)
        stale = _ref(store, "concept", "forgotten thing")
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET updated_at = now() - interval '400 days' "
                "WHERE ref_id = %s",
                (stale,),
            )
        links_before = _links(store)
        retired_before = _table_count(store, "refs WHERE retired_at IS NOT NULL")
        out = run_garden_tick(store, propose=_accept)
        assert out.proposals == 3
        actions = sorted(r["action"] for r in _garden_rows(store))
        assert actions == ["merge", "prune", "prune"]
        # Proposals only: not one ref or link changed.
        assert _links(store) == links_before
        assert _table_count(store, "refs WHERE retired_at IS NOT NULL") == (
            retired_before
        )
        # Idempotent: a second tick finds the same candidates pending.
        out2 = run_garden_tick(store, propose=_accept)
        assert out2.proposals == 0 and out2.skipped_pending == 3
        pending = pending_proposals(store)
        assert len(pending) == 3 and all(p.current for p in pending)
        # The merge proposal anchors on the loser (higher ref_id).
        assert {p.target_id for p in pending if p.action == "merge"} == {b}

    def test_decided_key_reopens_and_stale_marks_noncurrent(self, store: Store) -> None:
        stale = _ref(store, "concept", "forgotten thing")
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET updated_at = now() - interval '400 days' "
                "WHERE ref_id = %s",
                (stale,),
            )
        assert run_garden_tick(store, passes=("prune",), propose=_accept).proposals == 1
        (prop,) = pending_proposals(store)
        resolved = resolve_proposal(
            store, prop.review_id, actor="reto", verdict="rejected"
        )
        assert resolved is not None
        assert pending_proposals(store) == []
        # A rejected key stays closed while the target is unchanged
        # (gr504843) ...
        again = run_garden_tick(store, passes=("prune",), propose=_accept)
        assert again.proposals == 0 and again.skipped_rejected == 1
        # ... and re-opens once the target is edited (title is always
        # sha-covered): the next tick re-proposes it.
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET title = title || ' x', "
                "updated_at = now() - interval '400 days' WHERE ref_id = %s",
                (stale,),
            )
        assert run_garden_tick(store, passes=("prune",), propose=_accept).proposals == 1
        # A further edit makes the pending row non-current, not
        # pending-less.
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET title = title || ' y' WHERE ref_id = %s",
                (stale,),
            )
        (prop2,) = pending_proposals(store)
        assert prop2.current is False


# ── gr503580: bounded detectors — scan_cap + per-detector skip ──────────


class TestDetectorBounds:
    def test_untagged_honours_scan_cap(self, store: Store) -> None:
        """``scan_cap`` bounds the refs inspected, oldest-first: the seeder
        plus the first two untagged findings — never all four."""
        seeded = _ref(store, "finding", "tagged finding")
        store.add_tag(seeded, Tag.closed("DOMAIN", "materials"), set_by="system")
        for i in range(4):
            _ref(store, "finding", f"untagged {i}")
        hits = cand.untagged_candidates(
            store, namespaces=("DOMAIN",), kinds=("finding",), scan_cap=3
        )
        assert len(hits) == 2

    def test_untagged_needs_a_seeded_namespace(self, store: Store) -> None:
        for i in range(3):
            _ref(store, "finding", f"plain {i}")
        assert (
            cand.untagged_candidates(store, namespaces=("DOMAIN",), kinds=("finding",))
            == []
        )

    def test_cancelled_detector_is_skipped_and_the_rest_run(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr503580: a detector cancelled by its statement timeout is
        logged and skipped — the remaining detectors still produce."""
        stale = _ref(store, "concept", "forgotten thing")
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET updated_at = now() - interval '400 days' "
                "WHERE ref_id = %s",
                (stale,),
            )

        def _boom(*_a: Any, **_kw: Any) -> list[GardenCandidate]:
            raise QueryCanceled("canceling statement due to statement timeout")

        monkeypatch.setattr(cand, "untagged_candidates", _boom)
        out = run_garden_tick(store, passes=("prune", "tag-sweep"), propose=_accept)
        assert out.skipped_detectors == ["untagged"]
        assert out.proposals == 1

    def test_detector_past_its_budget_is_skipped(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wall-clock budget is per detector: still running at the
        deadline means abandoned and recorded in ``skipped``."""

        def _slow(*_a: Any, **_kw: Any) -> list[GardenCandidate]:
            time.sleep(0.3)
            return []

        monkeypatch.setattr(cand, "untagged_candidates", _slow)
        run = cand.run_detectors(store, passes=("tag-sweep",), detector_deadline_s=0.05)
        assert run.skipped == ["untagged"]
        assert run.candidates == []


# ── dispatch glue: gates per run, outcome contract, dedup across runs ──


class TestDispatchGlue:
    class _Ctx:
        """A minimal DispatchContext stand-in (all-callable)."""

        def __init__(self, store: Any, params: dict[str, Any]) -> None:
            self.store = store
            self.ref_id = 1
            self.title = "job"
            self.meta: dict[str, Any] = {"params": params}
            self.events: list[tuple[str, str]] = []
            self.failure: str | None = None
            self.deferred: str | None = None

        def append_chunk(self, kind: str, text: str) -> None:
            self.events.append((kind, text))

        def set_meta(self, **fields: Any) -> None:
            self.meta.update(fields)

        def record_failure(self, reason: str, **_kw: Any) -> None:
            self.failure = reason

        def defer(self, _delay: Any, reason: str) -> bool:
            self.deferred = reason
            return True

        def is_cancel_requested(self) -> bool:
            return False

    def test_registered_job_type(self) -> None:
        from precis.workers.job_types import get_job_type, known_job_types
        from precis.workers.job_types.graph_garden import SPEC

        spec = get_job_type("graph_garden")
        assert spec is not None and spec is SPEC
        assert "graph_garden" in known_job_types()
        assert spec.compatible_executors == frozenset({"job_inproc"})

    def test_dispatch_dark_service_gate_skips(self, store: Store) -> None:
        """The service_config gate is evaluated per run now (the mint
        that used to gate is gone): a dark service skips the tick and
        says so on meta.garden — no scanning, no failure."""
        from precis.workers.job_types.graph_garden import SPEC, _dispatch

        ctx = self._Ctx(store, {"pass": "all"})
        _dispatch(ctx, SPEC)
        assert ctx.meta["garden"]["skipped"] == "dark"
        assert ctx.meta["garden"]["proposals"] == 0
        assert ctx.events == [] and ctx.failure is None

    def test_dispatch_cooled_gate_skips(self, store: Store) -> None:
        """A dry streak of ``DRY_RUNS_ESCALATE`` runs cools the pass —
        the run gate-skips with the marker the cooling walk counts."""
        from precis.workers.job_types.graph_garden import SPEC, _dispatch

        _lit_service(store)
        for _ in range(DRY_RUNS_ESCALATE):
            _seed_garden_job(store, proposals=0, status="succeeded")
        ctx = self._Ctx(store, {"pass": "all"})
        _dispatch(ctx, SPEC)
        assert ctx.meta["garden"]["skipped"] == "cooled"
        assert ctx.events == [] and ctx.failure is None

    def test_dispatch_clean_tick_writes_no_chunk(self, store: Store) -> None:
        from precis.workers.job_types.graph_garden import SPEC, _dispatch

        _lit_service(store)
        ctx = self._Ctx(store, {"pass": "all"})
        _dispatch(ctx, SPEC)
        assert ctx.meta["garden"]["proposals"] == 0
        assert "skipped" not in ctx.meta["garden"]
        assert ctx.events == [] and ctx.failure is None

    def test_consecutive_runs_do_not_repropose(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ledger dedup holds across runs: two consecutive runs over
        the same graph produce proposals only on the first — a 12-a-day
        cadence must not spam the reviews ledger (or the proposer)."""
        from precis.workers.job_types.graph_garden import SPEC, _dispatch

        _lit_service(store)
        stale = _ref(store, "concept", "forgotten thing")
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET updated_at = now() - interval '400 days' "
                "WHERE ref_id = %s",
                (stale,),
            )
        monkeypatch.setattr("precis.graph.garden.proposer.propose_llm", _accept)
        ctx1 = self._Ctx(store, {"pass": "prune"})
        _dispatch(ctx1, SPEC)
        assert ctx1.meta["garden"]["proposals"] == 1
        ctx2 = self._Ctx(store, {"pass": "prune"})
        _dispatch(ctx2, SPEC)
        g2 = ctx2.meta["garden"]
        assert g2["proposals"] == 0 and g2["skipped_pending"] == 1
        assert "skipped" not in g2
        assert _table_count(store, "reviews") == 1

    def test_dispatch_paused_proposer_defers(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.workers.job_types.graph_garden import SPEC, _dispatch

        _lit_service(store)
        _ref(store, "concept", "forgotten thing")
        with store.pool.connection() as conn:
            conn.execute("UPDATE refs SET updated_at = now() - interval '400 days'")

        def _paused(_s: Any, _c: GardenCandidate) -> Verdict:
            return Verdict(accept=False, paused=True, error="quota")

        monkeypatch.setattr("precis.graph.garden.proposer.propose_llm", _paused)
        ctx = self._Ctx(store, {"pass": "prune"})
        _dispatch(ctx, SPEC)
        assert ctx.deferred is not None and ctx.failure is None

    def test_dispatch_summary_names_skipped_detectors(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr503580: a skipped detector lands in meta.garden AND the
        job_summary chunk names it — a partial tick is never silent."""
        from precis.workers.job_types.graph_garden import SPEC, _dispatch

        _lit_service(store)

        def _boom(*_a: Any, **_kw: Any) -> list[GardenCandidate]:
            raise QueryCanceled("canceling statement due to statement timeout")

        monkeypatch.setattr(cand, "untagged_candidates", _boom)
        ctx = self._Ctx(store, {"pass": "tag-sweep"})
        _dispatch(ctx, SPEC)
        assert ctx.meta["garden"]["skipped_detectors"] == ["untagged"]
        (kind, text) = ctx.events[0]
        assert kind == "job_summary" and "untagged" in text
        assert ctx.failure is None


# ── the recurring todo is the cadence (precis-recurring-help) ───────────


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _id_of(body: str) -> int:
    return int(body.split("id=")[1].split()[0].rstrip(",.()"))


def _set_last_tick(store: Store, ref_id: int, when: datetime) -> None:
    """A synthetic ``schedule:spawn`` event — the spawner's "last fired"
    marker (same helper shape as tests/test_schedule.py)."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_events (ref_id, source, event, ts, payload) "
            "VALUES (%s, 'schedule', 'spawn', %s, '{}'::jsonb)",
            (ref_id, when),
        )
        conn.commit()


class TestRecurringMintPath:
    def test_tick_spawns_child_carrying_garden_job_type(
        self, handler: TodoHandler, store: Store
    ) -> None:
        """The garden cadence is an operator-seeded recurring todo: a due
        tick mints a child carrying ``executor``/``job_type``/``params`` —
        the dispatch pass turns that child into the ``graph_garden`` job."""
        from precis.workers.schedule import run_schedule_pass

        resp = handler.put(
            text="Graph gardener",
            meta={
                "schedule": {"every": "2h"},
                "executor": "job_inproc",
                "job_type": "graph_garden",
                "params": {"pass": "all"},
            },
        )
        rid = _id_of(resp.body)
        _set_last_tick(store, rid, datetime.now(UTC) - timedelta(hours=3))
        assert run_schedule_pass(store, limit=50).ok >= 1
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta FROM refs WHERE parent_id = %s AND retired_at IS NULL",
                (rid,),
            ).fetchone()
        assert row is not None
        meta = dict(row[0])
        assert meta["job_type"] == "graph_garden"
        assert meta["executor"] == "job_inproc"
        assert meta["params"] == {"pass": "all"}
        assert meta["spawned_for_tick"] is not None


# ── dispatch dedupe (gr506771) and bounded retry (gr503581) ─────────────


def _mk_garden_job(store: Store, status: str = "queued", **meta_extra: Any) -> int:
    meta: dict[str, Any] = {
        "executor": "job_inproc",
        "job_type": "graph_garden",
        "params": {},
    }
    meta.update(meta_extra)
    ref = store.insert_ref(kind="job", slug=None, title="garden job", meta=meta)
    store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="system")
    return int(ref.id)


def _mk_tick_todo(handler: TodoHandler) -> int:
    resp = handler.put(
        text="garden tick",
        meta={"executor": "job_inproc", "job_type": "graph_garden"},
    )
    return _id_of(resp.body)


def test_dispatch_skips_tick_while_garden_job_pending(
    handler: TodoHandler, store: Store
) -> None:
    """gr506771: a recurring tick must not enqueue a second graph_garden
    job while an earlier one is still queued/running — the spawned todo
    is resolved 'skipped: previous pending', not minted."""
    from precis.workers.dispatch import _claim_and_dispatch

    _mk_garden_job(store, status="queued")
    tid = _mk_tick_todo(handler)

    claimed, minted, skipped = _claim_and_dispatch(store, tid)

    assert (claimed, minted, skipped) == (1, False, True)
    tags = {str(t) for t in store.tags_for(tid)}
    assert "STATUS:done" in tags
    with store.pool.connection() as conn:
        meta_row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (tid,)
        ).fetchone()
        assert meta_row is not None
        meta = meta_row[0]
        jobs_row = conn.execute(
            "SELECT count(*) FROM refs WHERE kind='job' "
            "AND meta->>'job_type' = 'graph_garden' AND retired_at IS NULL"
        ).fetchone()
        assert jobs_row is not None
        jobs = jobs_row[0]
    assert meta["skipped"] == "previous pending"
    assert jobs == 1  # still just the pending one


def test_dispatch_mints_normally_when_no_pending_job(
    handler: TodoHandler, store: Store
) -> None:
    """Control: with every earlier job terminal, the tick mints as before."""
    from precis.workers.dispatch import _claim_and_dispatch

    _mk_garden_job(store, status="failed")
    tid = _mk_tick_todo(handler)

    claimed, minted, skipped = _claim_and_dispatch(store, tid)

    assert (claimed, minted, skipped) == (1, True, False)


class _FakeCtx:
    def __init__(self, store: Store, ref_id: int, meta: dict[str, Any]) -> None:
        self.store = store
        self.ref_id = ref_id
        self.meta = meta


def test_failed_run_mints_one_bounded_retry(store: Store) -> None:
    """gr503581: a failed graph_garden job mints ONE sibling retry, parked
    ``waiting_time`` for the backoff; the retry itself carries
    ``meta.retry_of`` so a second failure mints nothing."""
    from precis.workers.job_types.graph_garden import _on_failed

    job_id = _mk_garden_job(store, status="failed")
    _on_failed(_FakeCtx(store, job_id, {"params": {"pass": "all"}}))

    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta FROM refs WHERE kind='job' "
            "AND meta->>'retry_of' = %s AND retired_at IS NULL",
            (str(job_id),),
        ).fetchall()
    assert len(rows) == 1
    retry_id, retry_meta = int(rows[0][0]), dict(rows[0][1])
    assert retry_meta["job_type"] == "graph_garden"
    assert retry_meta["wake_when"]["kind"] == "at_time"
    tags = {str(t) for t in store.tags_for(retry_id)}
    assert "STATUS:waiting_time" in tags

    # The retry's own failure mints nothing — the bound.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
            (f'{{"retry_of": {job_id}}}', retry_id),
        )
        conn.commit()
    _on_failed(_FakeCtx(store, retry_id, {"retry_of": job_id, "params": {}}))
    assert _retry_count(store) == 1


def _retry_count(store: Store) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE kind='job' "
            "AND meta->>'retry_of' IS NOT NULL AND retired_at IS NULL"
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_failed_run_no_retry_while_another_job_pending(store: Store) -> None:
    """A queued garden job already covers the next run — a failure does
    not stack a retry on top of it."""
    from precis.workers.job_types.graph_garden import _on_failed

    _mk_garden_job(store, status="queued")
    job_id = _mk_garden_job(store, status="failed")
    _on_failed(_FakeCtx(store, job_id, {"params": {}}))

    assert _retry_count(store) == 0

"""Tests for quest compute dispatch + the Pareto frontier — slice 4b.

Covers the pure Pareto logic (:mod:`precis.quest.frontier`), candidate
`structure` creation + content-addressing + `serves`/`candidate` wiring, the
harvest path (converged runs → `result` entries, idempotent; failed relax job →
`ruled-out`), and the tick's proposal handling (logged as hypotheses; compute
opt-in). Real relax dispatch is monkeypatched so no GPU compute runs. Runs
against real PG (the ``store`` fixture).
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest import compute as compute_mod
from precis.quest.frontier import (
    Candidate,
    _candidate_from_structure,
    pareto_split,
    quest_frontier,
)
from precis.quest.tick import run_quest_tick


def _mk_quest(store: Any, text: str) -> int:
    resp = QuestHandler(hub=Hub(store=store)).put(text=text)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _fake_dispatch(payload: dict[str, Any]) -> Any:
    def _d(_req: Any) -> Any:
        return SimpleNamespace(data=payload, text="", error=None, cost_usd=0.01)

    return _d


_SPEC = {
    "cell": {"a": 8.4, "b": 8.4, "c": 24.0, "pbc": [True, True, False]},
    "ops": [{"op": "add_atom", "element": "Fe", "frac": [0.0, 0.0, 0.5]}],
}


# ── pure Pareto ───────────────────────────────────────────────────────


class TestPareto:
    def test_lower_energy_dominates(self) -> None:
        a = Candidate(1, "st1", "A", {"energy": -10.0}, True)
        b = Candidate(2, "st2", "B", {"energy": -5.0}, True)
        fr = pareto_split([a, b], [("energy", "min")])
        assert [c.ref_id for c in fr.frontier] == [1]
        assert [c.ref_id for c in fr.dominated] == [2]

    def test_two_objectives_tradeoff_both_on_front(self) -> None:
        # a: lower energy, higher force; b: higher energy, lower force → neither
        # dominates (a trade-off), both on the frontier.
        a = Candidate(1, "st1", "A", {"energy": -10.0, "max_force": 0.9}, True)
        b = Candidate(2, "st2", "B", {"energy": -5.0, "max_force": 0.1}, True)
        fr = pareto_split([a, b], [("energy", "min"), ("max_force", "min")])
        assert len(fr.frontier) == 2 and not fr.dominated

    def test_unconverged_is_unevaluated(self) -> None:
        a = Candidate(1, "st1", "A", {}, False)
        fr = pareto_split([a], [("energy", "min")])
        assert fr.unevaluated and not fr.frontier


# ── candidate creation ────────────────────────────────────────────────


class TestEnsureCandidate:
    def test_creates_structure_serving_the_quest(self, store: Any) -> None:
        qid = _mk_quest(store, "A NO→NH₃ catalyst")
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe slab", "structure": _SPEC}
        )
        assert sid is not None
        # it is a structure, serving the quest, tagged candidate
        assert store.fetch_refs_by_ids({sid})[sid].kind == "structure"
        servers = store.links_for(qid, direction="in", relation="serves")
        assert sid in [ln.src_ref_id for ln in servers]
        assert any(str(t) == "candidate" for t in store.tags_for(sid))

    def test_content_addressed_dedup(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        s1 = compute_mod.ensure_candidate(store, qid, {"name": "x", "structure": _SPEC})
        s2 = compute_mod.ensure_candidate(store, qid, {"name": "x", "structure": _SPEC})
        assert s1 == s2  # same spec → same structure, a cache hit

    def test_no_structure_spec_returns_none(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        assert compute_mod.ensure_candidate(store, qid, {"name": "vague idea"}) is None


# ── harvest ───────────────────────────────────────────────────────────


class TestHarvest:
    def test_converged_run_becomes_result_entry_idempotently(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe", "structure": _SPEC}
        )
        assert sid is not None
        store.structure_record_run(
            sid,
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=42,
            max_disp=0.01,
            energy=-12.5,
            max_force=0.02,
        )
        step = compute_mod.harvest_measures(store, qid)
        assert step.results_harvested == 1
        logs = [
            b for b in store.list_blocks_for_ref(qid) if b.chunk_kind == "quest_log"
        ]
        assert any("E=-12.5 eV" in b.text for b in logs)
        # idempotent: a second harvest of the same run adds nothing
        step2 = compute_mod.harvest_measures(store, qid)
        assert step2.results_harvested == 0

    def test_failed_relax_job_rules_out_candidate(self, store: Any) -> None:
        from precis.store import Tag

        qid = _mk_quest(store, "A striving")
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe", "structure": _SPEC}
        )
        assert sid is not None
        # seed a failed struct_relax job under the candidate
        job = store.insert_ref(
            kind="job",
            slug=None,
            title="struct_relax",
            meta={"job_type": "struct_relax"},
            parent_id=sid,
        )
        store.add_tag(job.id, Tag.closed("STATUS", "failed"), set_by="system")
        step = compute_mod.harvest_measures(store, qid)
        assert step.ruled_out == 1
        assert any(str(t).startswith("ruled-out:") for t in store.tags_for(sid))


# ── frontier over the store ───────────────────────────────────────────


class TestQuestFrontier:
    def test_frontier_picks_lowest_energy_candidate(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        specs = [
            {
                "cell": {"a": 8.4, "b": 8.4, "c": 24.0},
                "ops": [{"op": "add_atom", "element": "Fe", "frac": [0.0, 0.0, 0.5]}],
            },
            {
                "cell": {"a": 8.4, "b": 8.4, "c": 24.0},
                "ops": [{"op": "add_atom", "element": "Co", "frac": [0.0, 0.0, 0.5]}],
            },
        ]
        ids = []
        for i, sp in enumerate(specs):
            sid = compute_mod.ensure_candidate(
                store, qid, {"name": f"c{i}", "structure": sp}
            )
            assert sid is not None
            ids.append(sid)
        store.structure_record_run(
            ids[0],
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=10,
            max_disp=0.0,
            energy=-20.0,
        )
        store.structure_record_run(
            ids[1],
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=10,
            max_disp=0.0,
            energy=-8.0,
        )
        fr = quest_frontier(store, qid)
        assert [c.ref_id for c in fr.frontier] == [ids[0]]
        assert [c.ref_id for c in fr.dominated] == [ids[1]]


# ── generalised frontier: arbitrary named measures (Slice 1) ──────────


def _cand(store: Any, sid: int) -> Candidate:
    ref = store.fetch_refs_by_ids({sid})[sid]
    return _candidate_from_structure(store, ref)


class TestGeneralizedFrontier:
    """The candidate's measures come from the run *and* ``structure.meta``, so a
    quest can rank on any named objective (e.g. a catpath ``barrier`` harvested
    onto the candidate) — not just the four relax columns."""

    def _two_candidates(self, store: Any) -> tuple[int, list[int]]:
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst")
        ids = []
        for i, elem in enumerate(("Fe", "Co")):
            sid = compute_mod.ensure_candidate(
                store,
                qid,
                {
                    "name": f"c{i}",
                    "structure": {
                        "cell": {"a": 8.4, "b": 8.4, "c": 24.0},
                        "ops": [
                            {"op": "add_atom", "element": elem, "frac": [0.0, 0.0, 0.5]}
                        ],
                    },
                },
            )
            assert sid is not None
            ids.append(sid)
        return qid, ids

    def test_ranks_on_barrier_from_meta_plus_energy_from_run(self, store: Any) -> None:
        # energy from the relax run, barrier stamped on structure.meta (the way a
        # harvested catpath result reaches the frontier). c0 wins on BOTH → sole
        # frontier; c1 dominated.
        qid, ids = self._two_candidates(store)
        store.stamp_ref_meta(
            qid,
            {
                "rubric_objectives": [
                    {"key": "energy", "sense": "min"},
                    {"key": "barrier", "sense": "min"},
                ]
            },
        )
        store.structure_record_run(
            ids[0],
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=10,
            max_disp=0.0,
            energy=-20.0,
        )
        store.structure_record_run(
            ids[1],
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=10,
            max_disp=0.0,
            energy=-8.0,
        )
        store.stamp_ref_meta(ids[0], {"barrier": 0.5})
        store.stamp_ref_meta(ids[1], {"barrier": 0.8})

        fr = quest_frontier(store, qid)
        assert fr.objectives == [("energy", "min"), ("barrier", "min")]
        assert [c.ref_id for c in fr.frontier] == [ids[0]]
        assert [c.ref_id for c in fr.dominated] == [ids[1]]

    def test_barrier_tradeoff_puts_both_on_front(self, store: Any) -> None:
        # c0 lower energy but higher barrier; c1 the reverse → neither dominates.
        qid, ids = self._two_candidates(store)
        store.stamp_ref_meta(
            qid,
            {
                "rubric_objectives": [
                    {"key": "energy", "sense": "min"},
                    {"key": "barrier", "sense": "min"},
                ]
            },
        )
        store.structure_record_run(
            ids[0],
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=10,
            max_disp=0.0,
            energy=-20.0,
        )
        store.structure_record_run(
            ids[1],
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=10,
            max_disp=0.0,
            energy=-8.0,
        )
        store.stamp_ref_meta(ids[0], {"barrier": 0.9})
        store.stamp_ref_meta(ids[1], {"barrier": 0.3})

        fr = quest_frontier(store, qid)
        assert {c.ref_id for c in fr.frontier} == {ids[0], ids[1]}
        assert not fr.dominated

    def test_missing_declared_objective_stays_unevaluated(self, store: Any) -> None:
        # A candidate with a converged relax but no barrier is NOT ranked when
        # the quest declares barrier — a catalyst isn't ranked until it's measured.
        qid, ids = self._two_candidates(store)
        store.stamp_ref_meta(
            qid, {"rubric_objectives": [{"key": "barrier", "sense": "min"}]}
        )
        store.structure_record_run(
            ids[0],
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=10,
            max_disp=0.0,
            energy=-20.0,
        )
        store.stamp_ref_meta(ids[0], {"barrier": 0.5})
        # ids[1]: relax converged but no barrier stamped
        store.structure_record_run(
            ids[1],
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=10,
            max_disp=0.0,
            energy=-8.0,
        )
        fr = quest_frontier(store, qid)
        assert [c.ref_id for c in fr.frontier] == [ids[0]]
        assert ids[1] in [c.ref_id for c in fr.unevaluated]

    def test_meta_measure_does_not_clobber_run_measure(self, store: Any) -> None:
        # Fill-only: a stray numeric meta key never overrides a real relax measure.
        qid, ids = self._two_candidates(store)
        store.structure_record_run(
            ids[0],
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=10,
            max_disp=0.0,
            energy=-20.0,
        )
        store.stamp_ref_meta(ids[0], {"energy": 999.0, "barrier": 0.5})
        c = _cand(store, ids[0])
        assert c.measures["energy"] == -20.0  # run wins
        assert c.measures["barrier"] == 0.5  # meta fills the gap

    def test_params_ride_along_but_are_not_measures(self, store: Any) -> None:
        qid, ids = self._two_candidates(store)
        store.stamp_ref_meta(ids[0], {"params": {"n_cu": 2, "facet": "111"}})
        c = _cand(store, ids[0])
        assert c.params == {"n_cu": 2, "facet": "111"}
        assert "params" not in c.measures  # the dict itself is never a measure


# ── by-total leaderboard view (§7.3) ──────────────────────────────────


class TestLeaderboard:
    def test_rows_ordered_banded_and_flagged(self) -> None:
        from precis.quest.frontier import FrontierResult, leaderboard

        f1 = Candidate(1, "st1", "A", {"barrier": 0.3, "energy": -20.0}, True)
        f2 = Candidate(
            2, "st2", "B", {"barrier": 0.9, "energy": -25.0}, True
        )  # tradeoff
        dom = Candidate(3, "st3", "C", {"barrier": 1.2, "energy": -5.0}, True)
        une = Candidate(4, "st4", "D", {}, False)
        fr = FrontierResult(
            objectives=[("barrier", "min"), ("energy", "min")],
            frontier=[f2, f1],  # deliberately unsorted input
            dominated=[dom],
            unevaluated=[une],
        )
        rows, schema = leaderboard(fr, graduated={1})
        assert schema == ["design", "name", "barrier", "energy", "band", "graduated"]
        # within the frontier, sorted by the primary objective (barrier, min)
        assert [r["design"] for r in rows] == ["st1", "st2", "st3", "st4"]
        assert [r["band"] for r in rows] == [
            "frontier",
            "frontier",
            "dominated",
            "awaiting",
        ]
        assert rows[0]["graduated"] == "★"  # st1 crossed the ceiling
        assert rows[1]["graduated"] == ""
        assert rows[3]["barrier"] == "—"  # unevaluated: no measure

    def test_view_leaderboard_renders_toon_table(self, store: Any) -> None:
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst")
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe slab", "structure": _SPEC}
        )
        assert sid is not None
        store.stamp_ref_meta(
            qid, {"rubric_objectives": [{"key": "barrier", "sense": "min"}]}
        )
        store.structure_record_run(
            sid,
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=5,
            max_disp=0.0,
            energy=-12.0,
        )
        store.stamp_ref_meta(sid, {"barrier": 0.42})

        body = QuestHandler(hub=Hub(store=store)).get(id=qid, view="leaderboard").body
        assert "leaderboard — quest" in body
        assert "barrier" in body and "band" in body  # TOON header columns
        assert "0.42" in body  # the measure cell
        assert "frontier" in body  # the Pareto band cell

    def test_view_leaderboard_empty_quest(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving with no candidates yet")
        body = QuestHandler(hub=Hub(store=store)).get(id=qid, view="leaderboard").body
        assert "no candidate structures serve this quest yet" in body


# ── catpath harvest: barrier → candidate meta → frontier (Slice 3) ────


class TestCatpathHarvest:
    def _candidate(self, store: Any, qid: int, name: str = "Pd") -> int:
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": name, "structure": _SPEC}
        )
        assert sid is not None
        return sid

    def _catpath_job(self, store: Any, sid: int, meta: dict[str, Any]) -> int:
        return store.insert_ref(
            kind="job",
            slug=None,
            title="catpath_explore",
            meta={"job_type": "catpath_explore", **meta},
            parent_id=sid,
        ).id

    def test_barrier_lands_on_meta_and_logs_result_idempotently(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst")
        sid = self._candidate(store, qid)
        self._catpath_job(store, sid, {"result": {"barrier": 0.7, "span": 1.2}})
        step = compute_mod.harvest_measures(store, qid)
        assert step.results_harvested == 1
        meta = store.fetch_refs_by_ids({sid})[sid].meta
        assert meta["barrier"] == 0.7 and meta["span"] == 1.2
        logs = [
            b for b in store.list_blocks_for_ref(qid) if b.chunk_kind == "quest_log"
        ]
        assert any("barrier=0.7" in b.text for b in logs)
        # idempotent: the same job is not re-harvested
        assert compute_mod.harvest_measures(store, qid).results_harvested == 0

    def test_barrier_feeds_the_frontier(self, store: Any) -> None:
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst")
        store.stamp_ref_meta(
            qid, {"rubric_objectives": [{"key": "barrier", "sense": "min"}]}
        )
        sid = self._candidate(store, qid)
        # a converged relax makes it evaluable; catpath supplies the barrier
        store.structure_record_run(
            sid,
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=5,
            max_disp=0.0,
            energy=-10.0,
        )
        self._catpath_job(store, sid, {"result": {"barrier": 0.5}})
        compute_mod.harvest_measures(store, qid)
        fr = quest_frontier(store, qid)
        assert [c.ref_id for c in fr.frontier] == [sid]  # ranked on the barrier

    def test_unfinished_job_contributes_nothing(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        self._catpath_job(store, sid, {})  # no barrier scalar yet → still running
        step = compute_mod.harvest_measures(store, qid)
        assert step.results_harvested == 0
        assert "barrier" not in (store.fetch_refs_by_ids({sid})[sid].meta or {})

    def test_pathway_link_created_when_ref_present(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        target = store.insert_ref(
            kind="job", slug=None, title="pw", meta={}, parent_id=sid
        ).id
        self._catpath_job(
            store, sid, {"result": {"barrier": 0.4}, "pathway_ref": target}
        )
        compute_mod.harvest_measures(store, qid)
        links = store.links_for(sid, direction="both", relation="related-to")
        linked = [ln.dst_ref_id for ln in links] + [ln.src_ref_id for ln in links]
        assert target in linked


def _catpath_registered() -> bool:
    """The `catpath_explore` job_type (and `pathway` kind) come from the catpath
    plugin — present in the dev container, absent on the torch-free host."""
    from precis.workers.job_types import get_job_type

    return get_job_type("catpath_explore") is not None


@pytest.mark.skipif(
    not _catpath_registered(), reason="catpath plugin not installed (host venv)"
)
class TestDispatchCatpath:
    """The candidate→catpath dispatch: mints a `catpath_explore` job pinned on
    the candidate (so :func:`harvest_measures` finds it) carrying the exported
    slab, plus the `pathway` write-back ref. The round-trip test closes the loop
    with the harvest half."""

    _RX = {"substrate": "NO", "target": "NH3", "network": "ammonia"}

    @pytest.fixture(autouse=True)
    def _catpath_schema(self, store: Any) -> None:
        """Guarantee the catpath plugin's `pathway` kind is registered on this
        worker's clone. A `fresh_db`-based migration test elsewhere in the suite
        rebuilds the clone with the plugin entry points monkeypatched out, which
        drops `pathway` — so ``insert_ref(kind='pathway')`` would then raise
        'unknown kind'. Re-applying migrations *through the plugin sources* is
        idempotent and restores the kind (a no-op when already present). Passing
        the bare dir would only load precis-core — ``discover_sources`` is what
        pulls in the catpath plugin migration."""
        from precis.store import Migrator
        from tests.conftest import MIGRATIONS_DIR, _active_dsn

        Migrator(_active_dsn(), Migrator.discover_sources(MIGRATIONS_DIR)).apply_all()

    def _candidate(self, store: Any, qid: int) -> int:
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Pd", "structure": _SPEC}
        )
        assert sid is not None
        return sid

    def test_mints_job_on_candidate_with_slab_and_pathway(self, store: Any) -> None:
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst")
        sid = self._candidate(store, qid)
        note = compute_mod.dispatch_catpath(store, sid, self._RX)
        assert note.startswith("catpath[")
        jobs = compute_mod._fresh_catpath_jobs(store, sid, 0)
        assert len(jobs) == 1
        _job_id, jmeta = jobs[0]
        params = jmeta.get("params") or {}
        # the exported slab rides along, provenance points back at the candidate,
        # and the reaction config is carried verbatim
        assert params["structure_ref"] == sid
        assert params["config"] == self._RX
        assert (
            isinstance(params["slab_extxyz"], str)
            and "Lattice=" in (params["slab_extxyz"])
        )
        # a pathway write-back ref was minted (status=computing)
        pw = store.get_ref(kind="pathway", id=params["pathway_slug"])
        assert pw is not None and pw.meta.get("candidate_ref") == sid

    def test_dispatch_is_idempotent(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        compute_mod.dispatch_catpath(store, sid, self._RX)
        compute_mod.dispatch_catpath(store, sid, self._RX)  # same geometry+config
        assert len(compute_mod._fresh_catpath_jobs(store, sid, 0)) == 1

    def test_roundtrip_dispatch_then_harvest(self, store: Any) -> None:
        """Dispatch mints a job the harvest can read back — the two halves wire
        together (the parent_id contract). Simulate the worker emitting a barrier
        onto the job meta, then harvest lifts it onto the candidate."""
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst")
        store.stamp_ref_meta(
            qid, {"rubric_objectives": [{"key": "barrier", "sense": "min"}]}
        )
        sid = self._candidate(store, qid)
        store.structure_record_run(
            sid,
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=5,
            max_disp=0.0,
            energy=-10.0,
        )
        compute_mod.dispatch_catpath(store, sid, self._RX)
        job_id, _jmeta = compute_mod._fresh_catpath_jobs(store, sid, 0)[0]
        # the ssh_node worker's dispatch emits the scalar summary onto the job meta
        store.stamp_ref_meta(job_id, {"barrier": 0.33, "span": 0.9})
        compute_mod.harvest_measures(store, qid)
        assert store.fetch_refs_by_ids({sid})[sid].meta["barrier"] == 0.33
        fr = quest_frontier(store, qid)
        assert [c.ref_id for c in fr.frontier] == [sid]  # ranked on the barrier

    def test_missing_structure_degrades(self, store: Any) -> None:
        note = compute_mod.dispatch_catpath(store, 999_999, self._RX)
        assert "skipped" in note and "not found" in note

    def test_empty_config_skipped(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        note = compute_mod.dispatch_catpath(store, sid, {})
        assert "skipped" in note
        assert compute_mod._fresh_catpath_jobs(store, sid, 0) == []


# ── tick integration ──────────────────────────────────────────────────


class TestTickProposals:
    def test_proposals_logged_as_hypotheses_without_compute(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        payload = {
            "logbook": [],
            "dossier_markdown": "",
            "proposals": [
                {"name": "Fe-N4", "rationale": "known active site", "structure": _SPEC},
                {"name": "vague", "rationale": "no structure"},
            ],
        }
        out = run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(payload))
        assert out.proposals == 2
        assert out.candidates_created == 0  # compute off
        logs = [
            b for b in store.list_blocks_for_ref(qid) if b.chunk_kind == "quest_log"
        ]
        assert any("Fe-N4" in b.text and "buildable" in b.text for b in logs)

    def test_compute_materialises_and_dispatches(
        self, store: Any, monkeypatch: Any
    ) -> None:
        calls: list[int] = []

        def _fake_relax(_store: Any, sid: int, **_kw: Any) -> str:
            calls.append(sid)
            return f"relax[ml] dispatched for {sid}"

        monkeypatch.setattr(compute_mod, "dispatch_relax", _fake_relax)
        qid = _mk_quest(store, "A striving")
        payload = {
            "logbook": [],
            "dossier_markdown": "",
            "proposals": [{"name": "Fe", "rationale": "x", "structure": _SPEC}],
        }
        out = run_quest_tick(
            store, qid, dispatch_fn=_fake_dispatch(payload), compute=True
        )
        assert out.candidates_created == 1
        assert out.sims_dispatched == 1
        assert len(calls) == 1  # relax was dispatched (stubbed)

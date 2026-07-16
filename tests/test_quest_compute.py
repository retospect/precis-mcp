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

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest import compute as compute_mod
from precis.quest.frontier import Candidate, pareto_split, quest_frontier
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

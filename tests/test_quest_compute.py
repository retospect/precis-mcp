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
    build_frontier_scatter,
    pareto_split,
    quest_frontier,
)
from precis.quest.tick import run_quest_tick
from precis.structure import preflight as preflight_mod


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


# ── frontier scatter — Cycle C J4 (quest hub v2) ────────────────────────


class TestFrontierScatter:
    def test_points_map_to_expected_coords(self) -> None:
        a = Candidate(1, "st1", "A", {"barrier": 0.3, "energy": -20.0}, True)
        b = Candidate(2, "st2", "B", {"barrier": 0.9, "energy": -10.0}, True)
        scatter = build_frontier_scatter([a, b])
        assert scatter is not None
        assert len(scatter.points) == 2
        by_id = {p["ref_id"]: p for p in scatter.points}
        assert by_id[1]["cx"] == 70.0
        assert by_id[1]["cy"] == 208.33
        assert by_id[2]["cx"] == 410.0
        assert by_id[2]["cy"] == 51.67
        assert scatter.x_min == 0.3 and scatter.x_max == 0.9
        assert scatter.y_min == -20.0 and scatter.y_max == -10.0

    def test_zero_candidates_is_none(self) -> None:
        assert build_frontier_scatter([]) is None

    def test_one_plottable_candidate_is_none(self) -> None:
        a = Candidate(1, "st1", "A", {"barrier": 0.3, "energy": -20.0}, True)
        assert build_frontier_scatter([a]) is None

    def test_missing_measure_candidate_excluded(self) -> None:
        # b is missing the y measure (`energy`) — not comparable ⇒ not
        # plottable, mirroring `_dominates`'s own "missing ⇒ not comparable"
        # rule. Only 1 remains plottable ⇒ below the 2-point floor ⇒ None.
        a = Candidate(1, "st1", "A", {"barrier": 0.3, "energy": -20.0}, True)
        b = Candidate(2, "st2", "B", {"barrier": 0.9}, True)
        assert build_frontier_scatter([a, b]) is None

    def test_missing_measure_candidate_excluded_but_others_plot(self) -> None:
        a = Candidate(1, "st1", "A", {"barrier": 0.3, "energy": -20.0}, True)
        b = Candidate(2, "st2", "B", {"barrier": 0.9, "energy": -10.0}, True)
        c = Candidate(3, "st3", "C", {"barrier": 0.5}, True)  # no `energy`
        scatter = build_frontier_scatter([a, b, c])
        assert scatter is not None
        assert {p["ref_id"] for p in scatter.points} == {1, 2}

    def test_all_equal_x_axis_no_divide_by_zero(self) -> None:
        a = Candidate(1, "st1", "A", {"barrier": 0.5, "energy": -20.0}, True)
        b = Candidate(2, "st2", "B", {"barrier": 0.5, "energy": -10.0}, True)
        scatter = build_frontier_scatter([a, b])
        assert scatter is not None
        cxs = {p["cx"] for p in scatter.points}
        # Both share x=0.5 → both land at the same, finite x pixel.
        assert len(cxs) == 1
        assert all(0.0 <= p["cx"] <= scatter.width for p in scatter.points)

    def test_all_equal_y_axis_no_divide_by_zero(self) -> None:
        a = Candidate(1, "st1", "A", {"barrier": 0.3, "energy": -15.0}, True)
        b = Candidate(2, "st2", "B", {"barrier": 0.9, "energy": -15.0}, True)
        scatter = build_frontier_scatter([a, b])
        assert scatter is not None
        cys = {p["cy"] for p in scatter.points}
        assert len(cys) == 1
        assert all(0.0 <= p["cy"] <= scatter.height for p in scatter.points)

    def test_open_url_for_stamps_per_point(self) -> None:
        a = Candidate(1, "st1", "A", {"barrier": 0.3, "energy": -20.0}, True)
        b = Candidate(2, "st2", "B", {"barrier": 0.9, "energy": -10.0}, True)
        scatter = build_frontier_scatter(
            [a, b], open_url_for=lambda c: f"/refs/structure/{c.ref_id}"
        )
        assert scatter is not None
        urls = {p["ref_id"]: p["open_url"] for p in scatter.points}
        assert urls == {1: "/refs/structure/1", 2: "/refs/structure/2"}

    def test_converged_flag_carried(self) -> None:
        a = Candidate(1, "st1", "A", {"barrier": 0.3, "energy": -20.0}, True)
        b = Candidate(2, "st2", "B", {"barrier": 0.9, "energy": -10.0}, False)
        scatter = build_frontier_scatter([a, b])
        assert scatter is not None
        conv = {p["ref_id"]: p["converged"] for p in scatter.points}
        assert conv == {1: True, 2: False}


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

    def test_slab_op_spec_materialises_without_a_top_level_cell(
        self, store: Any
    ) -> None:
        """A catalyst candidate is a `slab` op (no hand-enumerated cell) — the op
        builds the fcc(111) surface, so ensure_candidate must accept it."""
        pytest.importorskip("ase.build")
        qid = _mk_quest(store, "A NO→NH₃ catalyst")
        spec = {
            "ops": [{"op": "slab", "element": "Pd", "size": [2, 2, 3], "fix_layers": 1}]
        }
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Pd(111)", "structure": spec}
        )
        assert sid is not None
        scene, _ = store.structure_load(sid)
        assert len(scene.atoms) == 12
        assert {a.element for a in scene.atoms.values()} == {"Pd"}


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

    def test_relax_result_entry_stamped_by_system_not_the_caller_by(
        self, store: Any
    ) -> None:
        # gripes 171148/171149: a system-measured fact must be distinguishable
        # from model narration in the logbook, so it is ALWAYS stamped
        # by="system" — regardless of the caller's own `by` (the model's
        # "agent" attribution passed down from run_quest_tick).
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
            n_steps=10,
            max_disp=0.0,
            energy=-5.0,
        )
        compute_mod.harvest_measures(store, qid, by="agent")
        blocks = store.list_blocks_for_ref(qid)
        logs = [
            b
            for b in blocks
            if b.chunk_kind == "quest_log"
            and (b.meta or {}).get("entry_type") == "result"
        ]
        assert logs and all(b.meta["by"] == "system" for b in logs)

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
        dead_ends = [
            b
            for b in store.list_blocks_for_ref(qid)
            if b.chunk_kind == "quest_log"
            and (b.meta or {}).get("entry_type") == "dead-end"
        ]
        assert dead_ends and all(b.meta["by"] == "system" for b in dead_ends)

    def test_non_convergence_relax_failure_rules_out_candidate(
        self, store: Any
    ) -> None:
        """A relax job that ran to completion and reported a genuine physical
        failure (``failure_class="non-convergence"``) is a real verdict on the
        candidate — it still rules out, same as the untagged legacy case
        above."""
        from precis.store import Tag

        qid = _mk_quest(store, "A striving")
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe", "structure": _SPEC}
        )
        assert sid is not None
        job = store.insert_ref(
            kind="job",
            slug=None,
            title="struct_relax",
            meta={"job_type": "struct_relax", "failure_class": "non-convergence"},
            parent_id=sid,
        )
        store.add_tag(job.id, Tag.closed("STATUS", "failed"), set_by="system")
        step = compute_mod.harvest_measures(store, qid)
        assert step.ruled_out == 1
        assert any(str(t).startswith("ruled-out:") for t in store.tags_for(sid))

    def test_infra_relax_failure_does_not_rule_out_candidate(self, store: Any) -> None:
        """The real bug this pins: a `struct_relax` job that failed for an
        INFRA reason (container/docker/executor died — ``failure_class=
        "infra"``) must NOT be laundered into a dead-end verdict on the
        candidate's stability. It stays eligible for retry."""
        from precis.store import Tag

        qid = _mk_quest(store, "A striving")
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe", "structure": _SPEC}
        )
        assert sid is not None
        job = store.insert_ref(
            kind="job",
            slug=None,
            title="struct_relax",
            meta={"job_type": "struct_relax", "failure_class": "infra"},
            parent_id=sid,
        )
        store.add_tag(job.id, Tag.closed("STATUS", "failed"), set_by="system")
        step = compute_mod.harvest_measures(store, qid)
        assert step.ruled_out == 0
        assert not any(str(t).startswith("ruled-out:") for t in store.tags_for(sid))

    def test_infra_relax_failure_with_hub_retries_once(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR 0064 §C: given a hub, a candidate's first infra failure gets its
        relax re-dispatched (via ``dispatch_relax``, same cell), the retry
        counter set to 1, and it is NOT ruled out — this is the actual fix for
        "infra failure laundered into dry"."""
        from precis.store import Tag

        qid = _mk_quest(store, "A striving")
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe", "structure": _SPEC}
        )
        assert sid is not None
        job = store.insert_ref(
            kind="job",
            slug=None,
            title="struct_relax",
            meta={"job_type": "struct_relax", "failure_class": "infra"},
            parent_id=sid,
        )
        store.add_tag(job.id, Tag.closed("STATUS", "failed"), set_by="system")

        calls: list[dict[str, Any]] = []

        def _fake_relax(
            _s: Any, structure_ref_id: int, *, hub: Any = None, cell: Any = None
        ) -> str:
            calls.append(
                {"structure_ref_id": structure_ref_id, "hub": hub, "cell": cell}
            )
            return f"relax[ml] dispatched for {structure_ref_id}"

        monkeypatch.setattr(compute_mod, "dispatch_relax", _fake_relax)

        hub = object()
        step = compute_mod.harvest_measures(store, qid, hub=hub, relax_cell="inplane")
        assert step.ruled_out == 0
        assert not any(str(t).startswith("ruled-out:") for t in store.tags_for(sid))
        assert len(calls) == 1
        assert calls[0] == {"structure_ref_id": sid, "hub": hub, "cell": "inplane"}
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert meta.get("quest_infra_retries") == 1

    def test_infra_relax_failure_second_time_files_a_gripe_not_a_third_dispatch(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second consecutive infra failure (retry already used) files a
        bounded gripe instead of retrying again — never rules the candidate
        out (still no physical verdict), and a subsequent harvest doesn't
        re-file (dedup on the structure id)."""
        from precis.store import Tag

        qid = _mk_quest(store, "A striving")
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe", "structure": _SPEC}
        )
        assert sid is not None
        store.stamp_ref_meta(sid, {"quest_infra_retries": 1})  # already retried once
        job = store.insert_ref(
            kind="job",
            slug=None,
            title="struct_relax",
            meta={"job_type": "struct_relax", "failure_class": "infra"},
            parent_id=sid,
        )
        store.add_tag(job.id, Tag.closed("STATUS", "failed"), set_by="system")

        dispatch_calls: list[int] = []

        def _fake_dispatch_relax(_s: Any, sid: int, **_kw: Any) -> str:
            dispatch_calls.append(sid)
            return "relax[ml]"

        monkeypatch.setattr(compute_mod, "dispatch_relax", _fake_dispatch_relax)

        gripe_calls: list[dict[str, Any]] = []

        class _FakeGripeHandler:
            def __init__(self, *, hub: Any) -> None:
                self.hub = hub

            def put(self, *, text: str, tags: list[str] | None = None) -> None:
                gripe_calls.append({"text": text, "tags": tags})

        monkeypatch.setattr("precis.handlers.gripe.GripeHandler", _FakeGripeHandler)

        hub = object()
        step = compute_mod.harvest_measures(store, qid, hub=hub)
        assert step.ruled_out == 0
        assert not any(str(t).startswith("ruled-out:") for t in store.tags_for(sid))
        assert dispatch_calls == []  # no third dispatch
        assert len(gripe_calls) == 1
        assert f"quest {qid}" in gripe_calls[0]["text"]
        assert (
            f"structure:{sid}" in gripe_calls[0]["text"]
            or str(sid) in gripe_calls[0]["text"]
        )
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert meta.get("quest_infra_retries") == 2

        # a re-harvest while still failed does not re-file (dedup)
        step2 = compute_mod.harvest_measures(store, qid, hub=hub)
        assert step2.ruled_out == 0
        assert len(gripe_calls) == 1  # unchanged

    # ── autocatpath (barrier lane) — §C mirror; a crashed NEB never rules out ──

    def _reaction_quest(self, store: Any) -> int:
        """A quest with a reaction config so the autocatpath (barrier) lane is live."""
        qid = _mk_quest(store, "A NO→NH₃ catalyst")
        store.stamp_ref_meta(
            qid, {"reaction_config": {"substrate": "NO", "target": "NH3"}}
        )
        return qid

    def _failed_autocatpath(self, store: Any, sid: int) -> None:
        from precis.store import Tag

        job = store.insert_ref(
            kind="job",
            slug=None,
            title="autocatpath_explore",
            meta={"job_type": "autocatpath_explore"},
            parent_id=sid,
        )
        store.add_tag(job.id, Tag.closed("STATUS", "failed"), set_by="system")

    def test_failed_autocatpath_never_rules_out_candidate(self, store: Any) -> None:
        """A failed ``autocatpath_explore`` (a crashed NEB — a compute/infra failure)
        must NOT rule out: a barrier crash is never a physical verdict on the
        material, unlike a relax non-convergence (ADR 0064 §C). Note-only with
        no hub (dry preview)."""
        qid = self._reaction_quest(store)
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe", "structure": _SPEC}
        )
        assert sid is not None
        self._failed_autocatpath(store, sid)

        step = compute_mod.harvest_measures(store, qid)  # no hub → note-only
        assert step.ruled_out == 0
        assert not any(str(t).startswith("ruled-out:") for t in store.tags_for(sid))

    def test_failed_autocatpath_with_hub_retries_once(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Given a hub, a candidate's first autocatpath failure gets its barrier
        re-dispatched (via ``dispatch_autocatpath`` against the quest's reaction
        config), the per-lane counter set to 1, and it is NOT ruled out — the
        re-dispatch is what keeps the loop awaiting instead of reading the crash
        as a dry tick (ADR 0064 §C)."""
        qid = self._reaction_quest(store)
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe", "structure": _SPEC}
        )
        assert sid is not None
        self._failed_autocatpath(store, sid)

        calls: list[dict[str, Any]] = []

        def _fake_autocatpath(
            _s: Any,
            structure_ref_id: int,
            config: Any,
            *,
            hub: Any = None,
            force_backend: Any = None,
        ) -> str:
            calls.append(
                {"structure_ref_id": structure_ref_id, "hub": hub, "config": config}
            )
            return f"autocatpath[ml] dispatched for {structure_ref_id}"

        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_autocatpath)

        hub = object()
        step = compute_mod.harvest_measures(store, qid, hub=hub)
        assert step.ruled_out == 0
        assert not any(str(t).startswith("ruled-out:") for t in store.tags_for(sid))
        assert len(calls) == 1
        assert calls[0]["structure_ref_id"] == sid
        assert calls[0]["hub"] is hub
        assert calls[0]["config"] == {"substrate": "NO", "target": "NH3"}
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert meta.get("quest_autocatpath_infra_retries") == 1

    def test_failed_autocatpath_second_time_files_a_gripe_not_a_third_dispatch(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second consecutive autocatpath failure (retry already used) files a
        bounded ``autocatpath``-lane gripe instead of retrying again — never rules
        out, and a subsequent harvest doesn't re-file (dedup)."""
        qid = self._reaction_quest(store)
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe", "structure": _SPEC}
        )
        assert sid is not None
        store.stamp_ref_meta(
            sid, {"quest_autocatpath_infra_retries": 1}
        )  # retried once
        self._failed_autocatpath(store, sid)

        dispatch_calls: list[int] = []

        def _fake_dispatch_autocatpath(
            _s: Any, sid: int, _config: Any, **_kw: Any
        ) -> str:
            dispatch_calls.append(sid)
            return "autocatpath[ml]"

        monkeypatch.setattr(
            compute_mod, "dispatch_autocatpath", _fake_dispatch_autocatpath
        )

        gripe_calls: list[dict[str, Any]] = []

        class _FakeGripeHandler:
            def __init__(self, *, hub: Any) -> None:
                self.hub = hub

            def put(self, *, text: str, tags: list[str] | None = None) -> None:
                gripe_calls.append({"text": text, "tags": tags})

        monkeypatch.setattr("precis.handlers.gripe.GripeHandler", _FakeGripeHandler)

        hub = object()
        step = compute_mod.harvest_measures(store, qid, hub=hub)
        assert step.ruled_out == 0
        assert not any(str(t).startswith("ruled-out:") for t in store.tags_for(sid))
        assert dispatch_calls == []  # no third dispatch
        assert len(gripe_calls) == 1
        assert "autocatpath" in gripe_calls[0]["text"]
        assert f"quest {qid}" in gripe_calls[0]["text"]
        assert gripe_calls[0]["tags"] == ["quest-infra-failure"]
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert meta.get("quest_autocatpath_infra_retries") == 2

        # a re-harvest while still failed does not re-file (dedup)
        step2 = compute_mod.harvest_measures(store, qid, hub=hub)
        assert step2.ruled_out == 0
        assert len(gripe_calls) == 1  # unchanged


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
    quest can rank on any named objective (e.g. a autocatpath ``barrier`` harvested
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
        # harvested autocatpath result reaches the frontier). c0 wins on BOTH → sole
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

    def test_bookkeeping_meta_keys_excluded_from_measures(self, store: Any) -> None:
        # `version`/`quest_harvested_upto`/`quest_autocatpath_harvested_upto` are
        # structure_save/harvest bookkeeping, not ranking measures — they must
        # not pollute Candidate.measures alongside a real stamped measure.
        qid, ids = self._two_candidates(store)
        store.stamp_ref_meta(
            ids[0],
            {
                "barrier": 0.5,
                "version": 3,
                "quest_harvested_upto": 7,
                "quest_autocatpath_harvested_upto": 12,
            },
        )
        c = _candidate_from_structure(store, store.fetch_refs_by_ids({ids[0]})[ids[0]])
        assert c.measures.get("barrier") == 0.5
        assert "version" not in c.measures
        assert "quest_harvested_upto" not in c.measures
        assert "quest_autocatpath_harvested_upto" not in c.measures

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

    def test_untrusted_barrier_excluded_from_ranking_even_though_it_would_dominate(
        self, store: Any
    ) -> None:
        # ids[1]'s raw barrier (0.1) is far better than ids[0]'s (0.5) — if it
        # ranked, it would dominate. But its pathway didn't converge, so it must
        # be excluded from ranking entirely (Reto: "noise should be excluded
        # from ranking") — it lands in unevaluated, NOT dominated, NOT frontier.
        qid, ids = self._two_candidates(store)
        store.stamp_ref_meta(
            qid, {"rubric_objectives": [{"key": "barrier", "sense": "min"}]}
        )
        for sid, energy in zip(ids, (-20.0, -8.0)):
            store.structure_record_run(
                sid,
                fidelity="ml",
                on_version=1,
                converged=True,
                n_steps=10,
                max_disp=0.0,
                energy=energy,
            )
        store.stamp_ref_meta(ids[0], {"barrier": 0.5, "barrier_trusted": True})
        store.stamp_ref_meta(
            ids[1], {"barrier": 0.1, "barrier_trusted": False, "barrier_neb_failed": 2}
        )
        fr = quest_frontier(store, qid)
        assert [c.ref_id for c in fr.frontier] == [ids[0]]
        assert not fr.dominated
        assert [c.ref_id for c in fr.unevaluated] == [ids[1]]
        untrusted = next(c for c in fr.unevaluated if c.ref_id == ids[1])
        assert "barrier" not in untrusted.measures
        assert untrusted.flags["barrier_untrusted_value"] == 0.1
        assert untrusted.flags["barrier_trusted"] is False

    def test_all_untrusted_leaves_frontier_empty(self, store: Any) -> None:
        qid, ids = self._two_candidates(store)
        store.stamp_ref_meta(
            qid, {"rubric_objectives": [{"key": "barrier", "sense": "min"}]}
        )
        for sid, barrier in zip(ids, (0.5, 0.9)):
            store.structure_record_run(
                sid,
                fidelity="ml",
                on_version=1,
                converged=True,
                n_steps=10,
                max_disp=0.0,
                energy=-10.0,
            )
            store.stamp_ref_meta(sid, {"barrier": barrier, "barrier_trusted": False})
        fr = quest_frontier(store, qid)
        assert fr.frontier == []
        assert fr.dominated == []
        assert {c.ref_id for c in fr.unevaluated} == set(ids)


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
        assert schema == [
            "design",
            "name",
            "barrier",
            "energy",
            "band",
            "graduated",
            "quality",
        ]
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
        assert rows[0]["quality"] == ""  # no flags stamped → unknown, not flagged

    def test_untrusted_barrier_flagged_in_leaderboard(self) -> None:
        from precis.quest.frontier import FrontierResult, leaderboard

        f1 = Candidate(
            1,
            "st1",
            "A",
            {"barrier": 0.3},
            True,
            flags={"barrier_trusted": False},
        )
        fr = FrontierResult(
            objectives=[("barrier", "min")], frontier=[f1], dominated=[], unevaluated=[]
        )
        rows, _schema = leaderboard(fr)
        assert rows[0]["quality"] == "⚠ non-converged"

    def test_untrusted_barrier_shows_excluded_value_not_a_bare_dash(self) -> None:
        # An untrusted candidate has NO "barrier" in measures (excluded from
        # ranking at build time) but its flags carry the raw value — the
        # leaderboard should surface "0.648 (excluded)", not a bare "—".
        from precis.quest.frontier import FrontierResult, leaderboard

        f1 = Candidate(
            1,
            "st1",
            "A",
            {},  # barrier excluded from measures
            True,
            flags={"barrier_trusted": False, "barrier_untrusted_value": 0.648},
        )
        fr = FrontierResult(
            objectives=[("barrier", "min")], frontier=[], dominated=[], unevaluated=[f1]
        )
        rows, _schema = leaderboard(fr)
        assert rows[0]["barrier"] == "0.648 (excluded)"
        assert rows[0]["quality"] == "⚠ non-converged"

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


# ── autocatpath harvest: barrier → candidate meta → frontier (Slice 3) ────


class TestAutocatpathHarvest:
    def _candidate(self, store: Any, qid: int, name: str = "Pd") -> int:
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": name, "structure": _SPEC}
        )
        assert sid is not None
        return sid

    def _autocatpath_job(self, store: Any, sid: int, meta: dict[str, Any]) -> int:
        return store.insert_ref(
            kind="job",
            slug=None,
            title="autocatpath_explore",
            meta={"job_type": "autocatpath_explore", **meta},
            parent_id=sid,
        ).id

    def test_barrier_lands_on_meta_and_logs_result_idempotently(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst")
        sid = self._candidate(store, qid)
        self._autocatpath_job(store, sid, {"result": {"barrier": 0.7, "span": 1.2}})
        step = compute_mod.harvest_measures(store, qid)
        assert step.results_harvested == 1
        meta = store.fetch_refs_by_ids({sid})[sid].meta
        assert meta["barrier"] == 0.7 and meta["span"] == 1.2
        logs = [
            b for b in store.list_blocks_for_ref(qid) if b.chunk_kind == "quest_log"
        ]
        assert any("barrier=0.7" in b.text for b in logs)
        assert any(
            b.meta["by"] == "system"
            for b in logs
            if (b.meta or {}).get("entry_type") == "result"
        )
        # idempotent: the same job is not re-harvested
        assert compute_mod.harvest_measures(store, qid).results_harvested == 0

    def test_barrier_feeds_the_frontier(self, store: Any) -> None:
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst")
        store.stamp_ref_meta(
            qid, {"rubric_objectives": [{"key": "barrier", "sense": "min"}]}
        )
        sid = self._candidate(store, qid)
        # a converged relax makes it evaluable; autocatpath supplies the barrier
        store.structure_record_run(
            sid,
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=5,
            max_disp=0.0,
            energy=-10.0,
        )
        self._autocatpath_job(store, sid, {"result": {"barrier": 0.5}})
        compute_mod.harvest_measures(store, qid)
        fr = quest_frontier(store, qid)
        assert [c.ref_id for c in fr.frontier] == [sid]  # ranked on the barrier

    def test_unfinished_job_contributes_nothing(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        self._autocatpath_job(store, sid, {})  # no barrier scalar yet → still running
        step = compute_mod.harvest_measures(store, qid)
        assert step.results_harvested == 0
        assert "barrier" not in (store.fetch_refs_by_ids({sid})[sid].meta or {})

    def test_unfinished_job_is_retried_once_it_completes(self, store: Any) -> None:
        # The idempotency bookmark (quest_autocatpath_harvested_upto) used to
        # advance past a still-running job the moment it was scanned, so its
        # barrier was permanently lost once the job later finished.
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        job_id = self._autocatpath_job(store, sid, {})  # no scalar yet
        step = compute_mod.harvest_measures(store, qid)
        assert step.results_harvested == 0
        assert "barrier" not in (store.fetch_refs_by_ids({sid})[sid].meta or {})
        # the job now completes with a scalar barrier
        store.stamp_ref_meta(job_id, {"result": {"barrier": 0.5}})
        step2 = compute_mod.harvest_measures(store, qid)
        assert step2.results_harvested == 1
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert meta["barrier"] == 0.5

    def test_pathway_link_created_when_ref_present(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        target = store.insert_ref(
            kind="job", slug=None, title="pw", meta={}, parent_id=sid
        ).id
        self._autocatpath_job(
            store, sid, {"result": {"barrier": 0.4}, "pathway_ref": target}
        )
        compute_mod.harvest_measures(store, qid)
        links = store.links_for(sid, direction="both", relation="related-to")
        linked = [ln.dst_ref_id for ln in links] + [ln.src_ref_id for ln in links]
        assert target in linked

    def test_untrusted_barrier_flags_pathway_warnings(self, store: Any) -> None:
        """A pathway with a non-converged NEB + a desorbed adsorbate stamps
        ``barrier_trusted=False`` (+ counts) onto the candidate, so a garbage
        barrier never silently ranks as trustworthy."""
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        target = store.insert_ref(
            kind="job",
            slug=None,
            title="pw",
            meta={
                "warnings": [
                    "NO->N+O seed=0 NEB not converged",
                    "NH3 seed=0 geometry: adsorbate atom 41 detached from "
                    "slab (3.89 A)",
                ],
                "low_confidence": True,
            },
            parent_id=sid,
        ).id
        self._autocatpath_job(
            store, sid, {"result": {"barrier": 0.4}, "pathway_ref": target}
        )
        compute_mod.harvest_measures(store, qid)
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert meta["barrier_trusted"] is False
        assert meta["barrier_neb_failed"] == 1
        assert meta["barrier_desorbed"] == 1
        assert meta["barrier_low_confidence"] is True

    def test_clean_pathway_is_trusted(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        target = store.insert_ref(
            kind="job",
            slug=None,
            title="pw",
            meta={"warnings": [], "low_confidence": True},
            parent_id=sid,
        ).id
        self._autocatpath_job(
            store, sid, {"result": {"barrier": 0.4}, "pathway_ref": target}
        )
        compute_mod.harvest_measures(store, qid)
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert meta["barrier_trusted"] is True
        assert meta["barrier_neb_failed"] == 0
        assert meta["barrier_desorbed"] == 0
        assert meta["barrier_wrong_site"] == 0

    def test_wrong_site_pathway_is_untrusted(self, store: Any) -> None:
        """A bound-but-mis-bound endpoint (the ``*`` designates a different atom)
        flips ``barrier_trusted=False`` with a ``barrier_wrong_site`` count, so a
        barrier off a flipped geometry never ranks as trustworthy."""
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        target = store.insert_ref(
            kind="job",
            slug=None,
            title="pw",
            meta={
                "warnings": [
                    "NO seed=0 fragment NO (atoms 36-37) binds through O but the "
                    "* designates N — wrong-site",
                ],
                "low_confidence": False,
            },
            parent_id=sid,
        ).id
        self._autocatpath_job(
            store, sid, {"result": {"barrier": 0.3}, "pathway_ref": target}
        )
        compute_mod.harvest_measures(store, qid)
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert meta["barrier_trusted"] is False
        assert meta["barrier_wrong_site"] == 1
        assert meta["barrier_neb_failed"] == 0
        assert meta["barrier_desorbed"] == 0

    def test_adsorption_barrier_harvested_as_diagnostic(self, store: Any) -> None:
        """The tether's reseat adsorption barrier is harvested onto the candidate
        meta as an annotation (not a Pareto objective)."""
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        target = store.insert_ref(
            kind="job",
            slug=None,
            title="pw",
            meta={"warnings": [], "low_confidence": False},
            parent_id=sid,
        ).id
        self._autocatpath_job(
            store,
            sid,
            {
                "result": {"barrier": 0.4, "adsorption_barrier": 0.22},
                "pathway_ref": target,
            },
        )
        compute_mod.harvest_measures(store, qid)
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert meta["adsorption_barrier"] == 0.22
        assert meta["barrier"] == 0.4  # still the ranked measure

    def test_missing_pathway_ref_stamps_no_trust_flags(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        self._autocatpath_job(
            store, sid, {"result": {"barrier": 0.4}}
        )  # no pathway_ref
        step = compute_mod.harvest_measures(store, qid)
        assert step.results_harvested == 1
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert meta["barrier"] == 0.4
        assert "barrier_trusted" not in meta
        assert "barrier_neb_failed" not in meta


def _autocatpath_registered() -> bool:
    """The `autocatpath_explore` job_type (and `pathway` kind) come from the autocatpath
    plugin — present in the dev container, absent on the torch-free host."""
    from precis.workers.job_types import get_job_type

    return get_job_type("autocatpath_explore") is not None


@pytest.mark.skipif(
    not _autocatpath_registered(), reason="autocatpath plugin not installed (host venv)"
)
class TestAutocatpathContentKey:
    """The idem key folds an engine-version token so a redeployed autocatpath
    build re-keys (and re-scores) instead of reusing stale completed jobs."""

    _RX = {"substrate": "NO", "target": "NH3", "network": "ammonia"}
    _SLAB = 'Lattice="1 0 0 0 1 0 0 0 1"\nPd 0 0 0\n'

    def test_engine_token_defaults_to_cache_epoch(self, monkeypatch: Any) -> None:
        monkeypatch.delenv(compute_mod._AUTOCATPATH_VERSION_ENV, raising=False)
        assert (
            compute_mod._autocatpath_engine_token()
            == compute_mod._AUTOCATPATH_CACHE_EPOCH
        )

    def test_engine_token_prefers_env(self, monkeypatch: Any) -> None:
        monkeypatch.setenv(compute_mod._AUTOCATPATH_VERSION_ENV, "deadbeef")
        assert compute_mod._autocatpath_engine_token() == "deadbeef"

    def test_key_stable_for_same_engine(self, monkeypatch: Any) -> None:
        monkeypatch.setenv(compute_mod._AUTOCATPATH_VERSION_ENV, "0.4.0")
        k1 = compute_mod._autocatpath_content_key(self._RX, self._SLAB)
        k2 = compute_mod._autocatpath_content_key(self._RX, self._SLAB)
        assert k1 == k2

    def test_key_changes_with_engine_token(self, monkeypatch: Any) -> None:
        monkeypatch.setenv(compute_mod._AUTOCATPATH_VERSION_ENV, "0.1.1")
        old = compute_mod._autocatpath_content_key(self._RX, self._SLAB)
        monkeypatch.setenv(compute_mod._AUTOCATPATH_VERSION_ENV, "0.4.0")
        new = compute_mod._autocatpath_content_key(self._RX, self._SLAB)
        assert old != new


class TestDispatchAutocatpath:
    """The candidate→autocatpath dispatch: mints a `autocatpath_explore` job pinned on
    the candidate (so :func:`harvest_measures` finds it) carrying the exported
    slab, plus the `pathway` write-back ref. The round-trip test closes the loop
    with the harvest half."""

    _RX = {"substrate": "NO", "target": "NH3", "network": "ammonia"}

    @pytest.fixture(autouse=True)
    def _autocatpath_schema(self, store: Any) -> None:
        """Guarantee the autocatpath plugin's `pathway` kind is registered on this
        worker's clone. A `fresh_db`-based migration test elsewhere in the suite
        rebuilds the clone with the plugin entry points monkeypatched out, which
        drops `pathway` — so ``insert_ref(kind='pathway')`` would then raise
        'unknown kind'. Re-applying migrations *through the plugin sources* is
        idempotent and restores the kind (a no-op when already present). Passing
        the bare dir would only load precis-core — ``discover_sources`` is what
        pulls in the autocatpath plugin migration."""
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
        note = compute_mod.dispatch_autocatpath(store, sid, self._RX)
        assert note.startswith("autocatpath[")
        jobs = compute_mod._fresh_autocatpath_jobs(store, sid, 0)
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
        compute_mod.dispatch_autocatpath(store, sid, self._RX)
        compute_mod.dispatch_autocatpath(store, sid, self._RX)  # same geometry+config
        assert len(compute_mod._fresh_autocatpath_jobs(store, sid, 0)) == 1

    def test_engine_bump_forces_fresh_dispatch(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """A new autocatpath build (a changed engine token) must re-key so the
        candidate is re-scored instead of deduping onto the stale job — the fix
        for the qu164903 empty-frontier trap (21 candidates pinned on 0.1.1)."""
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        monkeypatch.setenv(compute_mod._AUTOCATPATH_VERSION_ENV, "0.1.1")
        compute_mod.dispatch_autocatpath(store, sid, self._RX)
        # Same geometry + config, but the engine was redeployed → new token.
        monkeypatch.setenv(compute_mod._AUTOCATPATH_VERSION_ENV, "0.4.0")
        compute_mod.dispatch_autocatpath(store, sid, self._RX)
        # Two distinct jobs — the second did NOT collapse onto the stale one.
        assert len(compute_mod._fresh_autocatpath_jobs(store, sid, 0)) == 2

    def test_redispatch_candidates_reevaluates_all_non_ruled_out(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """P0: after an engine bump, redispatch_candidates mints fresh jobs for
        every candidate (skipping ruled-out ones) instead of deduping onto the
        stale completed jobs."""
        from precis.store import Tag

        qid = _mk_quest(store, "Lowest-barrier Pd catalyst")
        store.stamp_ref_meta(qid, {"reaction_config": self._RX})
        # two DISTINCT candidates serving the quest; one gets ruled out
        spec2 = {
            "cell": {"a": 8.4, "b": 8.4, "c": 24.0, "pbc": [True, True, False]},
            "ops": [{"op": "add_atom", "element": "Ni", "frac": [0.0, 0.0, 0.5]}],
        }
        good = compute_mod.ensure_candidate(
            store, qid, {"name": "good", "structure": _SPEC}
        )
        bad = compute_mod.ensure_candidate(
            store, qid, {"name": "bad", "structure": spec2}
        )
        assert good is not None and bad is not None and good != bad
        store.add_tag(bad, Tag.open("ruled-out:preflight"), set_by="system")
        # initial dispatch under the old engine
        monkeypatch.setenv(compute_mod._AUTOCATPATH_VERSION_ENV, "0.1.1")
        compute_mod.dispatch_autocatpath(store, good, self._RX)
        # engine redeployed → re-dispatch re-keys and re-scores the good one only
        monkeypatch.setenv(compute_mod._AUTOCATPATH_VERSION_ENV, "0.4.0")
        note = compute_mod.redispatch_candidates(store, qid)
        assert "re-dispatched 1 candidate" in note
        assert len(compute_mod._fresh_autocatpath_jobs(store, good, 0)) == 2
        assert len(compute_mod._fresh_autocatpath_jobs(store, bad, 0)) == 0

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
        compute_mod.dispatch_autocatpath(store, sid, self._RX)
        job_id, _jmeta = compute_mod._fresh_autocatpath_jobs(store, sid, 0)[0]
        # the ssh_node worker's dispatch emits the scalar summary onto the job meta
        store.stamp_ref_meta(job_id, {"barrier": 0.33, "span": 0.9})
        compute_mod.harvest_measures(store, qid)
        assert store.fetch_refs_by_ids({sid})[sid].meta["barrier"] == 0.33
        fr = quest_frontier(store, qid)
        assert [c.ref_id for c in fr.frontier] == [sid]  # ranked on the barrier

    def test_missing_structure_degrades(self, store: Any) -> None:
        note = compute_mod.dispatch_autocatpath(store, 999_999, self._RX)
        assert "skipped" in note and "not found" in note

    def test_empty_config_skipped(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        note = compute_mod.dispatch_autocatpath(store, sid, {})
        assert "skipped" in note
        assert compute_mod._fresh_autocatpath_jobs(store, sid, 0) == []

    def test_routed_job_pins_cuda_device(self, store: Any, monkeypatch: Any) -> None:
        """A GPU-routed autocatpath job gets ``mlip.device=cuda`` injected (autocatpath
        defaults to cpu → the GPU sits idle otherwise); the caller's config dict
        is not mutated and its other keys are preserved."""
        monkeypatch.setenv(compute_mod._AUTOCATPATH_ROUTE_NODE_ENV, "spark")
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst")
        sid = self._candidate(store, qid)
        compute_mod.dispatch_autocatpath(store, sid, self._RX)
        _job_id, jmeta = compute_mod._fresh_autocatpath_jobs(store, sid, 0)[0]
        cfg = (jmeta.get("params") or {})["config"]
        assert cfg["mlip"]["device"] == "cuda"
        assert cfg["substrate"] == "NO"  # original keys ride along
        assert "mlip" not in self._RX  # caller's dict untouched

    def test_wall_seconds_env_reaches_the_job_and_the_ssh_node_lease(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """``PRECIS_AUTOCATPATH_WALL_SECONDS`` must reach the dispatched job's
        ``resources.wall_seconds`` (the field ``ssh_node``'s ``_lease_seconds``
        reads to size the lease) — a wiring regression, not just a unit check
        of either side in isolation."""
        from precis.workers.executors import ssh_node

        monkeypatch.setenv(compute_mod._AUTOCATPATH_WALL_SECONDS_ENV, "9000")
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        compute_mod.dispatch_autocatpath(store, sid, self._RX)
        _job_id, jmeta = compute_mod._fresh_autocatpath_jobs(store, sid, 0)[0]
        params = jmeta.get("params") or {}
        assert params["resources"]["wall_seconds"] == 9000
        # the full job meta (as stored) is what ssh_node's claim loop reads
        full_meta = {"params": params}
        assert ssh_node._lease_seconds(full_meta) == 9000 + ssh_node._LEASE_MARGIN_S

    def test_unrouted_job_leaves_device_unset(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """Without a route node there's no GPU to pin — the config is passed
        through verbatim (in-process EMT demo path)."""
        monkeypatch.delenv(compute_mod._AUTOCATPATH_ROUTE_NODE_ENV, raising=False)
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        compute_mod.dispatch_autocatpath(store, sid, self._RX)
        _job_id, jmeta = compute_mod._fresh_autocatpath_jobs(store, sid, 0)[0]
        assert (jmeta.get("params") or {})["config"] == self._RX  # unchanged

    # ── preflight hard gate (PRECIS_STRUCTURE_PREFLIGHT, default off) ──────

    _BAD_SPEC = {
        "cell": {"a": 8.4, "b": 8.4, "c": 24.0, "pbc": [True, True, False]},
        # He is a noble gas — outside MACE_MP_ELEMENTS (element_out_of_box).
        "ops": [{"op": "add_atom", "element": "He", "frac": [0.0, 0.0, 0.5]}],
    }

    def _bad_candidate(self, store: Any, qid: int) -> int:
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "He", "structure": self._BAD_SPEC}
        )
        assert sid is not None
        return sid

    def test_preflight_flag_off_dispatches_bad_candidate_regardless(
        self, store: Any, monkeypatch: Any
    ) -> None:
        monkeypatch.delenv(preflight_mod._PREFLIGHT_ENABLED_ENV, raising=False)
        qid = _mk_quest(store, "A striving")
        sid = self._bad_candidate(store, qid)
        note = compute_mod.dispatch_autocatpath(store, sid, self._RX)
        assert note.startswith(
            "autocatpath["
        )  # current behaviour: dispatches regardless

    def test_preflight_flag_on_skips_bad_candidate_and_stamps_dead_end(
        self, store: Any, monkeypatch: Any
    ) -> None:
        # Create the candidate with the gate OFF — `ensure_candidate` itself
        # calls `StructureHandler.put()` (seam 1), which would otherwise
        # reject this same bad geometry before it ever became a candidate.
        monkeypatch.delenv(preflight_mod._PREFLIGHT_ENABLED_ENV, raising=False)
        qid = _mk_quest(store, "A striving")
        sid = self._bad_candidate(store, qid)
        monkeypatch.setenv(preflight_mod._PREFLIGHT_ENABLED_ENV, "1")
        note = compute_mod.dispatch_autocatpath(store, sid, self._RX)
        assert "failed substrate preflight" in note
        assert compute_mod._fresh_autocatpath_jobs(store, sid, 0) == []  # no job minted
        tags = {str(t) for t in store.tags_for(sid)}
        assert any(t.startswith("ruled-out:preflight") for t in tags)

    def test_preflight_flag_on_dispatches_clean_candidate(
        self, store: Any, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv(preflight_mod._PREFLIGHT_ENABLED_ENV, "1")
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst")
        sid = self._candidate(store, qid)  # _SPEC — a single, in-box Fe atom
        note = compute_mod.dispatch_autocatpath(store, sid, self._RX)
        assert note.startswith("autocatpath[")
        assert len(compute_mod._fresh_autocatpath_jobs(store, sid, 0)) == 1


class TestReactionCoDispatch:
    """A barrier quest (``meta.reaction_config`` set) co-dispatches autocatpath with
    the relax for each new candidate; a plain quest dispatches relax only. Both
    dispatch fns are stubbed — no real compute, no `pathway` kind needed."""

    def _stub_both(self, monkeypatch: Any) -> tuple[list[int], list[tuple[int, dict]]]:
        relax_calls: list[int] = []
        autocatpath_calls: list[tuple[int, dict]] = []

        def _fake_relax(_store: Any, sid: int, **_kw: Any) -> str:
            relax_calls.append(sid)
            return f"relax[ml] dispatched for {sid}"

        def _fake_autocatpath(_store: Any, sid: int, cfg: dict, **_kw: Any) -> str:
            autocatpath_calls.append((sid, cfg))
            return f"autocatpath[emt] dispatched for {sid} → pathway p"

        monkeypatch.setattr(compute_mod, "dispatch_relax", _fake_relax)
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_autocatpath)
        return relax_calls, autocatpath_calls

    _RX = {"substrate": "NO", "target": "NH3", "network": "ammonia"}

    def test_reaction_quest_codispatches_autocatpath(
        self, store: Any, monkeypatch: Any
    ) -> None:
        relax_calls, autocatpath_calls = self._stub_both(monkeypatch)
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst for NO→NH₃")
        store.stamp_ref_meta(qid, {"reaction_config": self._RX})
        step = compute_mod.run_compute_step(
            store, qid, [{"name": "Pd", "structure": _SPEC}]
        )
        assert step.candidates_created == 1
        assert len(relax_calls) == 1
        # autocatpath fired for the same candidate, carrying the reaction config
        assert len(autocatpath_calls) == 1
        assert autocatpath_calls[0][0] == relax_calls[0]
        assert autocatpath_calls[0][1] == self._RX
        assert step.sims_dispatched == 2  # relax + autocatpath

    def test_plain_quest_dispatches_relax_only(
        self, store: Any, monkeypatch: Any
    ) -> None:
        relax_calls, autocatpath_calls = self._stub_both(monkeypatch)
        qid = _mk_quest(store, "A striving")  # no reaction_config
        step = compute_mod.run_compute_step(
            store, qid, [{"name": "Fe", "structure": _SPEC}]
        )
        assert len(relax_calls) == 1
        assert autocatpath_calls == []  # no reaction → no barrier lane
        assert step.sims_dispatched == 1

    def test_no_autocatpath_when_dispatch_off(
        self, store: Any, monkeypatch: Any
    ) -> None:
        relax_calls, autocatpath_calls = self._stub_both(monkeypatch)
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst")
        store.stamp_ref_meta(qid, {"reaction_config": self._RX})
        compute_mod.run_compute_step(
            store, qid, [{"name": "Pd", "structure": _SPEC}], dispatch=False
        )
        assert relax_calls == [] and autocatpath_calls == []  # preview: no compute

    def _stub_relax_cell(self, monkeypatch: Any) -> list[str | None]:
        """Capture the ``cell`` mode each relax dispatch is asked for."""
        seen: list[str | None] = []

        def _fake_relax(
            _s: Any, sid: int, *, cell: str | None = None, **_k: Any
        ) -> str:
            seen.append(cell)
            return f"relax[ml] dispatched for {sid}"

        monkeypatch.setattr(compute_mod, "dispatch_relax", _fake_relax)
        monkeypatch.setattr(
            compute_mod, "dispatch_autocatpath", lambda *a, **k: "autocatpath[emt] → p"
        )
        return seen

    def test_reaction_quest_relaxes_the_slab_box_inplane(
        self, store: Any, monkeypatch: Any
    ) -> None:
        # A barrier (slab) quest evaluates a *relaxed* slab: the relax frees the
        # box in-plane (vacuum pinned) rather than an atoms-only relax.
        seen = self._stub_relax_cell(monkeypatch)
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst for NO→NH₃")
        store.stamp_ref_meta(qid, {"reaction_config": self._RX})
        compute_mod.run_compute_step(store, qid, [{"name": "Pd", "structure": _SPEC}])
        assert seen == ["inplane"]

    def test_plain_quest_relax_stays_atoms_only(
        self, store: Any, monkeypatch: Any
    ) -> None:
        seen = self._stub_relax_cell(monkeypatch)
        qid = _mk_quest(store, "A striving")  # no reaction_config → no slab
        compute_mod.run_compute_step(store, qid, [{"name": "Fe", "structure": _SPEC}])
        assert seen == [None]


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

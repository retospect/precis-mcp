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
    _apply_rubric_composite,
    _candidate_from_structure,
    _rubric_composite_for,
    build_frontier_scatter,
    pareto_split,
    quest_frontier,
    render_frontier_tree,
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
        """Legacy flat shape: a failed ``autocatpath_explore`` job directly on
        the candidate — retired by the seed/aggregate fan-out (47332ad3);
        nothing mints these anymore, so a failure here is always stale-era
        (gr191615 amnesty)."""
        from precis.store import Tag

        job = store.insert_ref(
            kind="job",
            slug=None,
            title="autocatpath_explore",
            meta={"job_type": "autocatpath_explore"},
            parent_id=sid,
        )
        store.add_tag(job.id, Tag.closed("STATUS", "failed"), set_by="system")

    def _failed_autocatpath_aggregate(self, store: Any, sid: int) -> None:
        """Current fan-out shape: a failed ``autocatpath_aggregate`` job one
        level down, under the aggregate todo (``T_agg``, a direct child of the
        candidate) — mirrors what :func:`dispatch_autocatpath` mints (loosely,
        per ``_ensure_autocatpath_todo``'s shape)."""
        from precis.store import Tag

        agg_todo = store.insert_ref(
            kind="todo",
            slug=None,
            title="autocatpath aggregate",
            meta={"executor": "ssh_node", "job_type": "autocatpath_aggregate"},
            parent_id=sid,
        )
        job = store.insert_ref(
            kind="job",
            slug=None,
            title="autocatpath_aggregate",
            meta={"job_type": "autocatpath_aggregate"},
            parent_id=agg_todo.id,
        )
        store.add_tag(job.id, Tag.closed("STATUS", "failed"), set_by="system")

    def test_failed_autocatpath_never_rules_out_candidate(self, store: Any) -> None:
        """A failed legacy ``autocatpath_explore`` (retired pre-fan-out shape,
        47332ad3) exercises the gr191615 amnesty's note-only path (no hub —
        dry preview): it must NOT rule out, same as any autocatpath failure —
        a barrier crash is never a physical verdict on the material, unlike a
        relax non-convergence (ADR 0064 §C)."""
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
        """Given a hub, a candidate's first autocatpath failure (current-path
        ``autocatpath_aggregate``) gets its barrier re-dispatched (via
        ``dispatch_autocatpath`` against the quest's reaction config), the
        per-lane counter set to 1, and it is NOT ruled out — the re-dispatch is
        what keeps the loop awaiting instead of reading the crash as a dry
        tick (ADR 0064 §C)."""
        qid = self._reaction_quest(store)
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe", "structure": _SPEC}
        )
        assert sid is not None
        self._failed_autocatpath_aggregate(store, sid)

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
        """A second consecutive autocatpath failure (retry already used, current
        path ``autocatpath_aggregate``) files a bounded ``autocatpath``-lane
        gripe instead of retrying again — never rules out, and a subsequent
        harvest doesn't re-file (dedup)."""
        qid = self._reaction_quest(store)
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe", "structure": _SPEC}
        )
        assert sid is not None
        store.stamp_ref_meta(
            sid, {"quest_autocatpath_infra_retries": 1}
        )  # retried once
        self._failed_autocatpath_aggregate(store, sid)

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

    def test_stale_explore_failure_amnesty_redispatches_and_resets_counter(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr191615: a failed legacy ``autocatpath_explore`` job is always
        stale pre-fan-out signal, so it gets a one-shot amnesty even when the
        §C counter is already exhausted — bypasses the ladder (no gripe),
        re-dispatches via the current seed/aggregate path, and resets the
        counter to 0 for the fresh run's own full retry-once-then-gripe."""
        qid = self._reaction_quest(store)
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe", "structure": _SPEC}
        )
        assert sid is not None
        store.stamp_ref_meta(
            sid, {"quest_autocatpath_infra_retries": 2}
        )  # already exhausted by the dead poison-fail era
        self._failed_autocatpath(store, sid)

        dispatch_calls: list[dict[str, Any]] = []

        def _fake_dispatch_autocatpath(
            _s: Any, structure_ref_id: int, _config: Any, **_kw: Any
        ) -> str:
            dispatch_calls.append({"structure_ref_id": structure_ref_id})
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
        assert len(dispatch_calls) == 1
        assert dispatch_calls[0]["structure_ref_id"] == sid
        assert gripe_calls == []
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert meta.get("quest_autocatpath_infra_retries") == 0

    def test_latest_autocatpath_job_prefers_newer_aggregate_over_stale_explore(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A candidate with an older failed legacy ``autocatpath_explore`` AND
        a newer failed ``autocatpath_aggregate`` must be seen via the newer
        aggregate (``_latest_autocatpath_job``'s cross-shape ORDER BY) —
        harvest takes the counter-ladder path (dispatch + counter → 1), NOT
        the stale-era amnesty (which would reset the counter to 0)."""
        qid = self._reaction_quest(store)
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe", "structure": _SPEC}
        )
        assert sid is not None
        self._failed_autocatpath(store, sid)  # older, legacy shape
        self._failed_autocatpath_aggregate(store, sid)  # newer, current shape

        dispatch_calls: list[dict[str, Any]] = []

        def _fake_dispatch_autocatpath(
            _s: Any, structure_ref_id: int, _config: Any, **_kw: Any
        ) -> str:
            dispatch_calls.append({"structure_ref_id": structure_ref_id})
            return "autocatpath[ml]"

        monkeypatch.setattr(
            compute_mod, "dispatch_autocatpath", _fake_dispatch_autocatpath
        )

        hub = object()
        step = compute_mod.harvest_measures(store, qid, hub=hub)
        assert step.ruled_out == 0
        assert len(dispatch_calls) == 1
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert meta.get("quest_autocatpath_infra_retries") == 1


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

    def test_untrusted_barrier_also_excludes_selectivity_scalars_from_ranking(
        self, store: Any
    ) -> None:
        """The barrier's untrusted-value preservation (``flags[f"{k}_
        untrusted_value"]``) extends to the three selectivity/poisoning
        scalars — measured over the SAME untrusted pathway as the barrier, so
        `side_span_margin`/`trap_depth`/`poison_margin` are popped out of
        `measures` and their raw value preserved as a flag, exactly like
        `barrier`."""
        qid = _mk_quest(store, "A striving")
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Pd", "structure": _SPEC}
        )
        assert sid is not None
        store.stamp_ref_meta(
            sid,
            {
                "barrier": 0.4,
                "barrier_trusted": False,
                "side_span_margin": 0.35,
                "trap_depth": 0.7,
                "poison_margin": -0.42,
            },
        )
        c = _cand(store, sid)
        for k in ("side_span_margin", "trap_depth", "poison_margin"):
            assert k not in c.measures
        assert c.flags["side_span_margin_untrusted_value"] == 0.35
        assert c.flags["trap_depth_untrusted_value"] == 0.7
        assert c.flags["poison_margin_untrusted_value"] == -0.42

    def test_selectivity_naming_context_is_flags_only_never_a_measure(
        self, store: Any
    ) -> None:
        """``side_worst``/``trap_worst``/``poison_verdicts`` are naming
        context (a string or dict), never a ranking measure — they land in
        `Candidate.flags` and are excluded from `measures` by the `_numeric`
        filter, independent of the pathway's trust state."""
        qid = _mk_quest(store, "A striving")
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Pd", "structure": _SPEC}
        )
        assert sid is not None
        store.stamp_ref_meta(
            sid,
            {
                "side_worst": "N2O*",
                "trap_worst": "O*",
                "poison_verdicts": {"CO": "blocks", "H2O": "weak"},
            },
        )
        c = _cand(store, sid)
        assert c.flags["side_worst"] == "N2O*"
        assert c.flags["trap_worst"] == "O*"
        assert c.flags["poison_verdicts"] == {"CO": "blocks", "H2O": "weak"}
        for k in ("side_worst", "trap_worst", "poison_verdicts"):
            assert k not in c.measures

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
            "tier",
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

    def test_tier_glyph_column_reads_candidate_flag(self) -> None:
        """Tier-ladder UX item 4: the glyph column is ``TIER_GLYPH[flags['tier']]``
        — one char per rung, blank (not a fabricated glyph) for a candidate
        that never stamped a tier at all (pre-ladder / opted-out quest)."""
        from precis.quest.frontier import FrontierResult, leaderboard

        screening = Candidate(1, "st1", "A", {}, False, flags={"tier": "screening"})
        neb = Candidate(2, "st2", "B", {}, False, flags={"tier": "neb"})
        verify = Candidate(3, "st3", "C", {}, False, flags={"tier": "verify"})
        untiered = Candidate(4, "st4", "D", {}, False)
        fr = FrontierResult(
            objectives=[("energy", "min")],
            frontier=[],
            dominated=[],
            unevaluated=[screening, neb, verify, untiered],
        )
        rows, schema = leaderboard(fr)
        assert "tier" in schema
        by_design = {r["design"]: r["tier"] for r in rows}
        assert by_design == {"st1": "○", "st2": "◐", "st3": "●", "st4": ""}

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
        # Tier-glyph legend — a TOON cell has no title-attribute equivalent
        # for the glyph's own word, so it's spelled out once in the header.
        assert "○ screening" in body
        assert "◐ neb" in body
        assert "● verify" in body

    def test_view_leaderboard_empty_quest(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving with no candidates yet")
        body = QuestHandler(hub=Hub(store=store)).get(id=qid, view="leaderboard").body
        assert "no candidate structures serve this quest yet" in body


# ── composite rubric objective (pathway-potential-lever proposal, slice 2) ─


class TestRubricComposite:
    """``meta.rubric_composite`` = a human-set weighted-sum objective, computed
    at frontier-assembly time onto each candidate that has every weighted
    component present — referenceable from ``rubric_objectives`` like any
    other measure."""

    def test_composite_computed_when_all_components_present(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        store.stamp_ref_meta(
            qid,
            {
                "rubric_composite": {
                    "key": "score",
                    "weights": {"barrier": 1.0, "U_L_abs": 0.5},
                }
            },
        )
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Pd", "structure": _SPEC}
        )
        assert sid is not None
        store.stamp_ref_meta(sid, {"barrier": 0.4, "U_L_abs": 0.6})
        fr = quest_frontier(store, qid)
        c = next(
            x for x in fr.frontier + fr.dominated + fr.unevaluated if x.ref_id == sid
        )
        assert c.measures["score"] == pytest.approx(0.4 * 1.0 + 0.6 * 0.5)

    def test_composite_absent_when_a_component_is_missing(self, store: Any) -> None:
        """No partial sum, no fabrication: a candidate missing one weighted
        component gets no composite measure at all."""
        qid = _mk_quest(store, "A striving")
        store.stamp_ref_meta(
            qid,
            {
                "rubric_composite": {
                    "key": "score",
                    "weights": {"barrier": 1.0, "U_L_abs": 0.5},
                }
            },
        )
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Pd", "structure": _SPEC}
        )
        assert sid is not None
        store.stamp_ref_meta(sid, {"barrier": 0.4})  # no U_L_abs
        fr = quest_frontier(store, qid)
        c = next(
            x for x in fr.frontier + fr.dominated + fr.unevaluated if x.ref_id == sid
        )
        assert "score" not in c.measures

    def test_composite_referenceable_from_rubric_objectives_and_ranked(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        store.stamp_ref_meta(
            qid,
            {
                "rubric_composite": {"key": "score", "weights": {"barrier": 1.0}},
                "rubric_objectives": [{"key": "score", "sense": "min"}],
            },
        )
        sid_lo = compute_mod.ensure_candidate(
            store, qid, {"name": "lo", "structure": _SPEC}
        )
        sid_hi = compute_mod.ensure_candidate(
            store, qid, {"name": "hi", "structure": _SPEC}
        )
        assert sid_lo is not None and sid_hi is not None
        # a converged relax is what makes a candidate "evaluated" for pareto_split
        for sid in (sid_lo, sid_hi):
            store.structure_record_run(
                sid,
                fidelity="ml",
                on_version=1,
                converged=True,
                n_steps=5,
                max_disp=0.0,
                energy=-10.0,
            )
        store.stamp_ref_meta(sid_lo, {"barrier": 0.2})
        store.stamp_ref_meta(sid_hi, {"barrier": 0.9})
        fr = quest_frontier(store, qid)
        assert [c.ref_id for c in fr.frontier] == [sid_lo]  # minimises the composite

    def test_no_composite_declared_is_a_no_op(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Pd", "structure": _SPEC}
        )
        assert sid is not None
        store.stamp_ref_meta(sid, {"barrier": 0.4})
        fr = quest_frontier(store, qid)
        c = next(
            x for x in fr.frontier + fr.dominated + fr.unevaluated if x.ref_id == sid
        )
        assert "score" not in c.measures

    def test_render_frontier_tree_call_site_also_applies_composite(
        self, store: Any
    ) -> None:
        """The composite rides both frontier-assembly call sites. Verified
        via the private assembly helpers directly, since
        ``render_frontier_tree``'s printed line only ever shows the
        barrier/energy headline measure, never the composite key by name."""
        qid = _mk_quest(store, "A striving")
        store.stamp_ref_meta(
            qid,
            {"rubric_composite": {"key": "score", "weights": {"barrier": 2.0}}},
        )
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Pd", "structure": _SPEC}
        )
        assert sid is not None
        store.stamp_ref_meta(sid, {"barrier": 0.3})
        render_frontier_tree(store, qid)  # exercises the call site, must not raise

        c = _cand(store, sid)
        _apply_rubric_composite([c], _rubric_composite_for(store, qid))
        assert c.measures["score"] == pytest.approx(0.6)


def test_measures_from_job_lifts_selectivity_scalars_and_context_uncoerced() -> None:
    """`_autocatpath_measures_from_job` (the pure job-meta -> measures step
    `harvest_measures` calls) lifts the three selectivity/poisoning ranking
    scalars (numeric) alongside barrier, PLUS their naming context
    (side_worst/trap_worst/poison_verdicts) verbatim — strings/dicts, never
    coerced to a number."""
    meta = {
        "result": {
            "barrier": 0.4,
            "side_span_margin": 0.35,
            "trap_depth": 0.7,
            "poison_margin": -0.42,
            "side_worst": "N2O*",
            "trap_worst": "O*",
            "poison_verdicts": {"CO": "blocks", "H2O": "weak"},
            "limiting_factor": "poison",
            "worst_problem": "CO binds within -0.42 eV of the substrate",
        }
    }
    out = compute_mod._autocatpath_measures_from_job(meta)
    assert out["barrier"] == 0.4
    assert out["side_span_margin"] == 0.35
    assert out["trap_depth"] == 0.7
    assert out["poison_margin"] == -0.42
    assert out["side_worst"] == "N2O*"
    assert isinstance(out["side_worst"], str)
    assert out["trap_worst"] == "O*"
    assert out["poison_verdicts"] == {"CO": "blocks", "H2O": "weak"}
    assert isinstance(out["poison_verdicts"], dict)
    assert out["limiting_factor"] == "poison"
    assert out["worst_problem"].startswith("CO binds")


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

    def test_che_electro_scalars_harvested_onto_candidate_meta(
        self, store: Any
    ) -> None:
        """The four CHE electro scalars (U_L, U_opt, span_at_Uopt, P_side)
        ride the same job-meta -> candidate-meta harvest path as barrier/
        span, plus a derived U_L_abs; span_at_UL is NOT lifted (job-meta-only
        diagnostic)."""
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        self._autocatpath_job(
            store,
            sid,
            {
                "result": {
                    "barrier": 0.4,
                    "U_L": -0.9,
                    "U_opt": -0.7,
                    "span_at_UL": 1.3,
                    "span_at_Uopt": 1.1,
                    "P_side": 0.05,
                }
            },
        )
        compute_mod.harvest_measures(store, qid)
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert meta["U_L"] == -0.9
        assert meta["U_L_abs"] == 0.9
        assert meta["U_opt"] == -0.7
        assert meta["span_at_Uopt"] == 1.1
        assert meta["P_side"] == 0.05
        assert "span_at_UL" not in meta

    def test_untrusted_pathway_excludes_electro_scalars_from_ranking(
        self, store: Any
    ) -> None:
        """The SAME trust gate that excludes an untrusted barrier from
        ranking also excludes U_L/U_L_abs/U_opt/span_at_Uopt/P_side — raw
        values preserved on the candidate's flags, never on `measures`."""
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        target = store.insert_ref(
            kind="job",
            slug=None,
            title="pw",
            meta={
                "warnings": ["NO->N+O seed=0 NEB not converged"],
                "low_confidence": False,
            },
            parent_id=sid,
        ).id
        self._autocatpath_job(
            store,
            sid,
            {
                "result": {
                    "barrier": 0.4,
                    "U_L": -0.9,
                    "U_opt": -0.7,
                    "span_at_Uopt": 1.1,
                    "P_side": 0.05,
                },
                "pathway_ref": target,
            },
        )
        compute_mod.harvest_measures(store, qid)
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        # still stamped onto the candidate's raw meta (harvest is unconditional)
        assert meta["U_L"] == -0.9 and meta["barrier_trusted"] is False

        c = _cand(store, sid)
        for k in (
            "barrier",
            "span",
            "U_L",
            "U_L_abs",
            "U_opt",
            "span_at_Uopt",
            "P_side",
        ):
            assert k not in c.measures
        assert c.flags["barrier_untrusted_value"] == 0.4
        assert c.flags["U_L_untrusted_value"] == -0.9
        assert c.flags["U_opt_untrusted_value"] == -0.7
        assert c.flags["span_at_Uopt_untrusted_value"] == 1.1
        assert c.flags["P_side_untrusted_value"] == 0.05

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


class TestTierLadderHarvest:
    """The tier-ladder half of harvest: a completed screening run stamps
    ``tier`` with no barrier at all (catpath omits it — nothing here
    special-cases the absence); a neb→verify supersession preserves the
    outgoing parked barrier as ``barrier_screen`` and tracks ``barrier_tier``;
    a landed verify pathway wires ``refines`` back to its parked sibling."""

    def _candidate(self, store: Any, qid: int) -> int:
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Pd", "structure": _SPEC}
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

    def test_screening_completion_stamps_tier_with_no_barrier(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        pw = store.insert_ref(
            kind="job",
            slug=None,
            title="pw-screening",
            meta={"tier": "screening", "warnings": [], "low_confidence": False},
            parent_id=sid,
        ).id
        # a screening (relax-only) run: no `ea`/`barrier`, but catpath's own
        # CHE electrochemistry scalars still land (see compute.py's
        # `_AUTOCATPATH_ELECTRO_KEYS` docstring — computed independent of
        # any NEB).
        self._autocatpath_job(store, sid, {"result": {"U_L": -0.6}, "pathway_ref": pw})
        step = compute_mod.harvest_measures(store, qid)
        assert step.results_harvested == 1
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert "barrier" not in meta
        assert meta["tier"] == "screening"

    def test_neb_barrier_stamps_barrier_tier_neb(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        pw = store.insert_ref(
            kind="job",
            slug=None,
            title="pw",
            meta={"tier": "neb", "warnings": [], "low_confidence": False},
            parent_id=sid,
        ).id
        self._autocatpath_job(
            store, sid, {"result": {"barrier": 0.5}, "pathway_ref": pw}
        )
        compute_mod.harvest_measures(store, qid)
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert meta["barrier"] == 0.5
        assert meta["barrier_tier"] == "neb"
        assert meta["tier"] == "neb"
        assert "barrier_screen" not in meta

    def test_verify_supersedes_neb_moves_parked_value_to_barrier_screen(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        parked_pw = store.insert_ref(
            kind="job",
            slug=None,
            title="pw-neb",
            meta={"tier": "neb", "warnings": [], "low_confidence": False},
            parent_id=sid,
        ).id
        self._autocatpath_job(
            store, sid, {"result": {"barrier": 0.5}, "pathway_ref": parked_pw}
        )
        compute_mod.harvest_measures(store, qid)  # neb barrier lands first

        verify_pw = store.insert_ref(
            kind="job",
            slug=None,
            title="pw-verify",
            meta={"tier": "verify", "warnings": [], "low_confidence": False},
            parent_id=sid,
        ).id
        self._autocatpath_job(
            store, sid, {"result": {"barrier": 0.3}, "pathway_ref": verify_pw}
        )
        compute_mod.harvest_measures(store, qid)  # verify supersedes

        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert meta["barrier"] == 0.3  # canonical = highest fidelity
        assert meta["barrier_screen"] == 0.5  # superseded parked value kept
        assert meta["barrier_tier"] == "verify"
        assert meta["tier"] == "verify"

    def test_barrier_screen_excluded_from_ranking_measures(self, store: Any) -> None:
        """`barrier_screen` is calibration data, never a Pareto objective —
        mirrors `adsorption_barrier`'s existing treatment."""
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        store.stamp_ref_meta(
            sid, {"barrier": 0.3, "barrier_screen": 0.5, "barrier_tier": "verify"}
        )
        c = _cand(store, sid)
        assert "barrier_screen" not in c.measures
        assert c.flags["barrier_screen"] == 0.5
        assert c.flags["barrier_tier"] == "verify"


class TestTierLadderRefinesLink:
    """A landed verify(coadsorbed)-tier pathway wires `refines` back to its
    parked(neb)-tier sibling on the same candidate — a higher-fidelity
    treatment of the same object."""

    @pytest.fixture(autouse=True)
    def _pathway_schema(self, store: Any) -> None:
        """`_find_tier_pathway` queries ``kind='pathway'`` — needs the
        autocatpath plugin's `pathway` kind registered (its migration, not
        the ``autocatpath`` compute package itself — precis_pathway is glue
        code that always ships with this repo)."""
        from precis.store import Migrator
        from tests.conftest import MIGRATIONS_DIR, _active_dsn

        Migrator(_active_dsn(), Migrator.discover_sources(MIGRATIONS_DIR)).apply_all()

    def _candidate(self, store: Any, qid: int) -> int:
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Pd", "structure": _SPEC}
        )
        assert sid is not None
        return sid

    def _pathway(self, store: Any, sid: int, tier: str) -> int:
        return store.insert_ref(
            kind="pathway",
            slug=f"pw-{tier}-{sid}",
            title=f"pw-{tier}",
            meta={
                "candidate_ref": sid,
                "tier": tier,
                "warnings": [],
                "low_confidence": False,
            },
            parent_id=sid,
        ).id

    def _autocatpath_job(self, store: Any, sid: int, meta: dict[str, Any]) -> int:
        return store.insert_ref(
            kind="job",
            slug=None,
            title="autocatpath_explore",
            meta={"job_type": "autocatpath_explore", **meta},
            parent_id=sid,
        ).id

    def test_verify_links_refines_to_parked_sibling(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        parked_pw = self._pathway(store, sid, "neb")
        self._autocatpath_job(
            store, sid, {"result": {"barrier": 0.5}, "pathway_ref": parked_pw}
        )
        compute_mod.harvest_measures(store, qid)

        verify_pw = self._pathway(store, sid, "verify")
        self._autocatpath_job(
            store, sid, {"result": {"barrier": 0.3}, "pathway_ref": verify_pw}
        )
        compute_mod.harvest_measures(store, qid)

        links = store.links_for(verify_pw, direction="out", relation="refines")
        assert [ln.dst_ref_id for ln in links] == [parked_pw]

    def test_no_parked_sibling_is_a_silent_noop(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        verify_pw = self._pathway(store, sid, "verify")
        self._autocatpath_job(
            store, sid, {"result": {"barrier": 0.3}, "pathway_ref": verify_pw}
        )
        step = compute_mod.harvest_measures(store, qid)
        assert step.results_harvested == 1  # harvest still succeeds
        assert store.links_for(verify_pw, direction="out", relation="refines") == []


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


class TestTierConfigMapping:
    """:func:`compute._apply_tier_config` — the tier→catpath-config overlay
    (pure, no store): screening = relax-only ranking (``search.screening``
    + ``template=parked``), verify = the same NEB search over
    ``template=coadsorbed``, neb = identity (today's default, byte-
    identical — the ladder-off legacy path)."""

    _CFG = {"substrate": "NO", "target": "NH3", "search": {"seeds": [0, 1, 2]}}

    def test_screening_sets_search_screening_and_parked_template(self) -> None:
        out = compute_mod._apply_tier_config(self._CFG, compute_mod._TIER_SCREENING)
        assert out["search"]["screening"] is True
        assert out["template"] == "parked"
        # other search keys survive the overlay
        assert out["search"]["seeds"] == [0, 1, 2]
        assert out["substrate"] == "NO"

    def test_screening_does_not_mutate_the_caller_dict(self) -> None:
        cfg = {"search": {"seeds": [0]}}
        compute_mod._apply_tier_config(cfg, compute_mod._TIER_SCREENING)
        assert cfg == {"search": {"seeds": [0]}}

    def test_verify_sets_coadsorbed_template_leaves_search_untouched(self) -> None:
        out = compute_mod._apply_tier_config(self._CFG, compute_mod._TIER_VERIFY)
        assert out["template"] == "coadsorbed"
        assert out["search"] == self._CFG["search"]

    def test_neb_tier_is_the_ladder_off_legacy_identity_path(self) -> None:
        out = compute_mod._apply_tier_config(self._CFG, compute_mod._TIER_NEB)
        assert out is self._CFG  # same object — no overlay, no copy
        assert "template" not in out

    def test_unrecognized_tier_falls_back_to_the_neb_identity_path(self) -> None:
        out = compute_mod._apply_tier_config(self._CFG, "bogus-tier")
        assert out is self._CFG


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

    @pytest.fixture(autouse=True)
    def _autocatpath_seed_job_types(self, monkeypatch: Any) -> None:
        """Inject the §B-1 ``autocatpath_seed`` / ``autocatpath_aggregate``
        job_types into the registry for the test — same reason
        ``test_pathway_plugin.py``'s ``register_autocatpath_explore`` fixture
        does this for ``autocatpath_explore``: the dev container's installed
        entry_points.txt is a snapshot from image build time, so a pyproject
        entry-point added mid-worktree isn't live without a reinstall.
        ``monkeypatch.setitem`` auto-reverts, so this doesn't leak into other
        test modules."""
        pytest.importorskip("autocatpath")
        from precis.workers import job_types as jt
        from precis_pathway import aggregate_job, seed_job

        monkeypatch.setitem(jt._REGISTRY, "autocatpath_seed", seed_job.SPEC)
        monkeypatch.setitem(jt._REGISTRY, "autocatpath_aggregate", aggregate_job.SPEC)

    def _candidate(self, store: Any, qid: int) -> int:
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Pd", "structure": _SPEC}
        )
        assert sid is not None
        return sid

    def _seed_jobs(self, store: Any, sid: int) -> list[tuple[int, dict]]:
        """``(job_id, meta)`` for every ``autocatpath_seed`` job under EVERY
        aggregate tree :func:`dispatch_autocatpath` minted under candidate
        ``sid`` (oldest first) — the seed level of the §B-1 fan-out tree
        (candidate -> T_agg -> per-seed todo -> seed job)."""
        with store.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT j.ref_id, j.meta FROM refs seed_todo
                  JOIN refs j ON j.parent_id = seed_todo.ref_id
                              AND j.kind = 'job' AND j.deleted_at IS NULL
                 WHERE seed_todo.kind = 'todo' AND seed_todo.deleted_at IS NULL
                   AND seed_todo.parent_id IN (
                         SELECT ref_id FROM refs
                          WHERE parent_id = %s AND kind = 'todo'
                            AND deleted_at IS NULL
                       )
                   AND j.meta->>'job_type' = 'autocatpath_seed'
                 ORDER BY j.ref_id
                """,
                (sid,),
            ).fetchall()
        return [(int(r[0]), dict(r[1] or {})) for r in rows]

    def _agg_todo_ids(self, store: Any, sid: int) -> list[int]:
        """Every T_agg (aggregate todo) minted directly under candidate
        ``sid`` — one per distinct :func:`dispatch_autocatpath` tree
        (a repeat dispatch with a bumped engine token mints a second)."""
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ref_id FROM refs WHERE parent_id = %s AND kind = 'todo' "
                "AND deleted_at IS NULL ORDER BY ref_id",
                (sid,),
            ).fetchall()
        return [int(r[0]) for r in rows]

    def _promote_aggregate(self, store: Any, sid: int) -> int:
        """Drive the async half of ONE tree to completion: stamp every seed
        job succeeded, run the auto_check pass (closes each per-seed todo),
        then the dispatch pass (mints T_agg's own ``autocatpath_aggregate``
        job now that no seed todo is live under it). Returns that job's ref
        id — callers stamp a scalar barrier onto it to simulate its own
        (ssh_node) dispatch, same as the harvest tests always have."""
        from precis.store import Tag
        from precis.workers.auto_check import run_auto_check_pass
        from precis.workers.dispatch import run_dispatch_pass

        for job_id, _m in self._seed_jobs(store, sid):
            store.stamp_ref_meta(
                job_id,
                {"partial": {"seed": 0, "states": {}, "steps": {}, "warnings": []}},
            )
            store.add_tag(
                job_id,
                Tag.closed("STATUS", "succeeded"),
                set_by="system",
                replace_prefix=True,
            )
        run_auto_check_pass(store)  # per-seed todos -> STATUS:done
        run_dispatch_pass(store)  # T_agg now eligible -> mints its own job
        jobs = compute_mod._fresh_autocatpath_jobs(store, sid, 0)
        assert jobs, "autocatpath_aggregate job did not mint"
        return jobs[0][0]

    def test_mints_job_on_candidate_with_slab_and_pathway(self, store: Any) -> None:
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst")
        sid = self._candidate(store, qid)
        note = compute_mod.dispatch_autocatpath(store, sid, self._RX)
        assert note.startswith("autocatpath[")
        # default search.seeds=[0,1,2], one mlip spec -> 3 seed jobs
        seed_jobs = self._seed_jobs(store, sid)
        assert len(seed_jobs) == 3
        for _job_id, jmeta in seed_jobs:
            params = jmeta.get("params") or {}
            # the exported slab rides along and the reaction config is
            # carried verbatim on every seed unit
            assert params["config"] == self._RX
            assert (
                isinstance(params["slab_extxyz"], str)
                and "Lattice=" in (params["slab_extxyz"])
            )
            assert params["model_index"] == 0
        assert {jmeta["params"]["seed"] for _jid, jmeta in seed_jobs} == {0, 1, 2}
        # a pathway write-back ref was minted (status=computing), addressed
        # off the aggregate todo's own params (not any individual seed's).
        (agg_id,) = self._agg_todo_ids(store, sid)
        agg_params = (store.fetch_refs_by_ids({agg_id})[agg_id].meta or {}).get(
            "params"
        ) or {}
        pw = store.get_ref(kind="pathway", id=agg_params["pathway_slug"])
        assert pw is not None and pw.meta.get("candidate_ref") == sid

    def test_dispatch_is_idempotent(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        compute_mod.dispatch_autocatpath(store, sid, self._RX)
        compute_mod.dispatch_autocatpath(store, sid, self._RX)  # same geometry+config
        # retry skips seeds whose todo already exists — still 3, not 6
        assert len(self._seed_jobs(store, sid)) == 3
        assert len(self._agg_todo_ids(store, sid)) == 1  # one tree, not two

    def test_default_tier_is_neb_config_and_pathway_meta_unchanged(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        compute_mod.dispatch_autocatpath(store, sid, self._RX)  # no tier= given
        seed_jobs = self._seed_jobs(store, sid)
        assert seed_jobs
        for _jid, jmeta in seed_jobs:
            assert jmeta["params"]["config"] == self._RX  # byte-identical, no overlay
        (agg_id,) = self._agg_todo_ids(store, sid)
        agg_params = (store.fetch_refs_by_ids({agg_id})[agg_id].meta or {}).get(
            "params"
        ) or {}
        pw = store.get_ref(kind="pathway", id=agg_params["pathway_slug"])
        assert pw is not None and pw.meta.get("tier") == "neb"

    def test_screening_tier_overlays_config_and_stamps_the_pathway(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        compute_mod.dispatch_autocatpath(
            store, sid, self._RX, tier=compute_mod._TIER_SCREENING
        )
        seed_jobs = self._seed_jobs(store, sid)
        assert seed_jobs
        for _jid, jmeta in seed_jobs:
            cfg = jmeta["params"]["config"]
            assert cfg["search"]["screening"] is True
            assert cfg["template"] == "parked"
        (agg_id,) = self._agg_todo_ids(store, sid)
        agg_params = (store.fetch_refs_by_ids({agg_id})[agg_id].meta or {}).get(
            "params"
        ) or {}
        pw = store.get_ref(kind="pathway", id=agg_params["pathway_slug"])
        assert pw is not None and pw.meta.get("tier") == "screening"

    def test_screening_and_neb_tiers_content_address_onto_distinct_pathways(
        self, store: Any
    ) -> None:
        """The tier overlay folds into the content key, so a promotion (a
        fresh dispatch at a different tier) mints its OWN job/pathway tree
        rather than clobbering the prior tier's."""
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        compute_mod.dispatch_autocatpath(
            store, sid, self._RX, tier=compute_mod._TIER_SCREENING
        )
        compute_mod.dispatch_autocatpath(
            store, sid, self._RX, tier=compute_mod._TIER_NEB
        )
        assert len(self._agg_todo_ids(store, sid)) == 2

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
        # Two distinct trees (2 aggregate todos, 3 seeds each) — the second
        # tree did NOT collapse onto the stale one.
        assert len(self._agg_todo_ids(store, sid)) == 2
        assert len(self._seed_jobs(store, sid)) == 6

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
        assert len(self._agg_todo_ids(store, good)) == 2
        assert len(self._seed_jobs(store, good)) == 6
        assert self._agg_todo_ids(store, bad) == []

    def test_candidate_ids_ignore_non_structure_serves_links(self, store: Any) -> None:
        """A quest's `serves` in-links mix structures with papers/dossier/todos;
        redispatch + reset act on structures only (a paper has no slab to export)."""
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst")
        sid = self._candidate(store, qid)
        other = _mk_quest(store, "some other ref")  # a non-structure serves-link
        with store.tx() as conn:
            store.add_link(
                src_ref_id=other, dst_ref_id=qid, relation="serves", conn=conn
            )
        assert compute_mod._candidate_struct_ids(store, qid) == [sid]

    def test_reset_compute_wipes_stale_history_keeps_designs(self, store: Any) -> None:
        """reset_compute nulls stale barrier measures + drops ruled-out and
        graduation tags for a clean re-run, without deleting the candidate."""
        from precis.store import Tag

        qid = _mk_quest(store, "Lowest-barrier Pd catalyst")
        store.stamp_ref_meta(qid, {"reaction_config": self._RX})
        sid = self._candidate(store, qid)
        # simulate a stale, untrusted, graduated candidate
        store.stamp_ref_meta(
            sid,
            {
                "barrier": 0.64,
                "barrier_trusted": False,
                "barrier_neb_failed": 3,
                "energy": -179.6,
                "quest_autocatpath_harvested_upto": 42,
            },
        )
        store.add_tag(sid, Tag.open("ruled-out:preflight"), set_by="system")
        store.add_tag(sid, Tag.open("needs-experiment"), set_by="system")

        note = compute_mod.reset_compute(store, qid)
        assert "reset 1 candidate" in note

        meta = store.fetch_refs_by_ids({sid})[sid].meta
        assert meta.get("barrier") is None  # stale barrier nulled
        assert meta.get("barrier_trusted") is None
        assert meta.get("energy") == -179.6  # relax lane untouched
        # bookmark PRESERVED — nulling it to 0 would re-harvest the stale
        # completed job and re-stamp the barrier this reset just cleared.
        assert meta.get("quest_autocatpath_harvested_upto") == 42
        tags = {str(t) for t in store.tags_for(sid)}
        assert not any(t.startswith("ruled-out:") for t in tags)  # rule-out dropped
        assert "needs-experiment" not in tags  # false graduation dropped

    def test_roundtrip_dispatch_then_harvest(self, store: Any) -> None:
        """Dispatch mints a tree the harvest can read back once the seed fan-
        out resolves — the two halves wire together (the parent_id contract,
        now via the aggregate todo). Simulate every seed succeeding (auto_check
        + dispatch pass promote the aggregate job) and the ssh_node worker's
        aggregate dispatch emitting a barrier onto ITS meta, then harvest lifts
        it onto the candidate."""
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
        job_id = self._promote_aggregate(store, sid)
        # the ssh_node worker's aggregate dispatch emits the scalar summary
        # onto the job meta
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
        _job_id, jmeta = self._seed_jobs(store, sid)[0]
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
        _job_id, jmeta = self._seed_jobs(store, sid)[0]
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
        _job_id, jmeta = self._seed_jobs(store, sid)[0]
        assert (jmeta.get("params") or {})["config"] == self._RX  # unchanged

    # ── gr172886: capability-map route resolution + null-route guard ───────

    def test_capability_default_routes_to_gpu_host(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """With the env unset, a GPU-advertising host in resource_slots is
        picked as the route node (deterministic, lowest-sorted GPU host) —
        the out-of-daemon caller (e.g. ``precis quest redispatch`` from a
        plain shell) no longer null-routes onto random EMT."""
        monkeypatch.delenv(compute_mod._AUTOCATPATH_ROUTE_NODE_ENV, raising=False)
        store.sync_host_resource_slots("spark", {"gpu": 1})
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst")
        sid = self._candidate(store, qid)
        note = compute_mod.dispatch_autocatpath(store, sid, self._RX)
        assert note.startswith("autocatpath[")
        _job_id, jmeta = self._seed_jobs(store, sid)[0]
        params = jmeta.get("params") or {}
        assert params["target_node"] == "spark"
        assert params["force_backend"] != "emt"  # routed → the config's own backend
        assert params["config"]["mlip"]["device"] == "cuda"

    def test_null_route_raises_on_multihost_cluster_without_gpu(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """gr172886: env unset AND no host advertises `gpu`, but resource_slots
        spans a real multi-node cluster — this is a prod misconfiguration, not
        dev. Refuse loudly instead of silently minting an unrouted EMT job."""
        monkeypatch.delenv(compute_mod._AUTOCATPATH_ROUTE_NODE_ENV, raising=False)
        store.sync_host_resource_slots("caspar", {"cpu": 4})
        store.sync_host_resource_slots("melchior", {"cpu": 4})
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst")
        sid = self._candidate(store, qid)
        with pytest.raises(RuntimeError, match="gr172886"):
            compute_mod.dispatch_autocatpath(store, sid, self._RX)
        assert compute_mod._fresh_autocatpath_jobs(store, sid, 0) == []

    def test_dev_emt_path_preserved_when_resource_slots_empty(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """Env unset AND resource_slots empty (dev/CI, no cluster to misroute
        onto) — falls through to the pre-existing unrouted EMT dispatch, no
        raise."""
        monkeypatch.delenv(compute_mod._AUTOCATPATH_ROUTE_NODE_ENV, raising=False)
        assert store.all_resource_slots() == []
        qid = _mk_quest(store, "A striving")
        sid = self._candidate(store, qid)
        compute_mod.dispatch_autocatpath(store, sid, self._RX)
        _job_id, jmeta = self._seed_jobs(store, sid)[0]
        params = jmeta.get("params") or {}
        assert params["target_node"] is None
        assert params["force_backend"] == "emt"

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
        assert len(self._seed_jobs(store, sid)) == 3


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

    def test_ladder_on_quest_dispatches_screening_tier_first(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """``meta.tier_ladder=True`` steers a NEW candidate's first
        autocatpath run to the cheap screening rung, not straight to neb."""
        tier_calls: list[Any] = []

        def _fake_autocatpath(_store: Any, sid: int, cfg: dict, **kw: Any) -> str:
            tier_calls.append(kw.get("tier"))
            return f"autocatpath[emt] dispatched for {sid} → pathway p"

        monkeypatch.setattr(
            compute_mod, "dispatch_relax", lambda *a, **k: "relax[ml] dispatched"
        )
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_autocatpath)
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst for NO→NH₃")
        store.stamp_ref_meta(qid, {"reaction_config": self._RX, "tier_ladder": True})
        compute_mod.run_compute_step(store, qid, [{"name": "Pd", "structure": _SPEC}])
        assert tier_calls == [compute_mod._TIER_SCREENING]

    def test_ladder_off_quest_dispatches_neb_tier_by_default(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """No ``meta.tier_ladder`` (the pre-ladder default, and every
        existing quest/test) keeps today's straight-to-NEB dispatch."""
        tier_calls: list[Any] = []

        def _fake_autocatpath(_store: Any, sid: int, cfg: dict, **kw: Any) -> str:
            tier_calls.append(kw.get("tier"))
            return f"autocatpath[emt] dispatched for {sid} → pathway p"

        monkeypatch.setattr(
            compute_mod, "dispatch_relax", lambda *a, **k: "relax[ml] dispatched"
        )
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_autocatpath)
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst for NO→NH₃")
        store.stamp_ref_meta(qid, {"reaction_config": self._RX})  # ladder unset
        compute_mod.run_compute_step(store, qid, [{"name": "Pd", "structure": _SPEC}])
        assert tier_calls == [compute_mod._TIER_NEB]

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


class TestTierPromotion:
    """`promote_tiers` — code-driven, no LLM surface: screening→neb and
    neb→verify, each capped + ranked by the quest's rubric, skipping a
    candidate that already has that next-tier pathway dispatched. Real
    ``dispatch_autocatpath`` is stubbed (mirrors `TestReactionCoDispatch`) —
    only the *selection* logic is under test here."""

    _RX = {"substrate": "NO", "target": "NH3", "network": "ammonia"}

    @pytest.fixture(autouse=True)
    def _pathway_schema(self, store: Any) -> None:
        """The promotion-eligibility filter (`_find_tier_pathway`) queries
        ``kind='pathway'`` — needs the plugin's `pathway` kind registered
        (its migration, not the ``autocatpath`` compute package)."""
        from precis.store import Migrator
        from tests.conftest import MIGRATIONS_DIR, _active_dsn

        Migrator(_active_dsn(), Migrator.discover_sources(MIGRATIONS_DIR)).apply_all()

    def _quest(self, store: Any, **extra_meta: Any) -> int:
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst for NO→NH₃")
        store.stamp_ref_meta(
            qid, {"reaction_config": self._RX, "tier_ladder": True, **extra_meta}
        )
        return qid

    @staticmethod
    def _spec_for(name: str) -> dict[str, Any]:
        """A candidate spec content-addressed uniquely per `name` — distinct
        candidates need distinct specs (:func:`compute._candidate_slug`
        hashes the spec alone, not the proposal's `name`)."""
        z = 0.01 * len(name)
        return {
            "cell": {"a": 8.4, "b": 8.4, "c": 24.0, "pbc": [True, True, False]},
            "ops": [{"op": "add_atom", "element": "Fe", "frac": [0.0, 0.0, z]}],
        }

    def _screening_candidate(
        self, store: Any, qid: int, name: str, u_l_abs: float
    ) -> int:
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": name, "structure": self._spec_for(name)}
        )
        assert sid is not None
        store.stamp_ref_meta(sid, {"tier": "screening", "U_L_abs": u_l_abs})
        return sid

    def _neb_frontier_candidate(
        self, store: Any, qid: int, name: str, barrier: float, energy: float
    ) -> int:
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": name, "structure": self._spec_for(name)}
        )
        assert sid is not None
        store.structure_record_run(
            sid,
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=5,
            max_disp=0.0,
            energy=energy,
        )
        store.stamp_ref_meta(
            sid, {"barrier": barrier, "barrier_trusted": True, "barrier_tier": "neb"}
        )
        return sid

    def _stub_dispatch(self, monkeypatch: Any) -> list[tuple[int, Any]]:
        calls: list[tuple[int, Any]] = []

        def _fake(_store: Any, sid: int, _cfg: dict, **kw: Any) -> str:
            calls.append((sid, kw.get("tier")))
            return f"autocatpath[emt] dispatched for {sid} → pathway p"

        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake)
        return calls

    def test_ladder_off_is_a_noop(self, store: Any, monkeypatch: Any) -> None:
        calls = self._stub_dispatch(monkeypatch)
        qid = _mk_quest(store, "A striving")
        store.stamp_ref_meta(qid, {"reaction_config": self._RX})  # no tier_ladder
        self._screening_candidate(store, qid, "A", 0.5)
        assert compute_mod.promote_tiers(store, qid) == []
        assert calls == []

    def test_no_reaction_config_is_a_noop(self, store: Any, monkeypatch: Any) -> None:
        calls = self._stub_dispatch(monkeypatch)
        qid = _mk_quest(store, "A striving")
        store.stamp_ref_meta(qid, {"tier_ladder": True})  # no reaction_config
        assert compute_mod.promote_tiers(store, qid) == []
        assert calls == []

    def test_promotes_up_to_cap_ranked_best_first(
        self, store: Any, monkeypatch: Any
    ) -> None:
        calls = self._stub_dispatch(monkeypatch)
        qid = self._quest(store, tier_promote_neb=2, tier_promote_verify=0)
        store.stamp_ref_meta(
            qid, {"rubric_objectives": [{"key": "U_L_abs", "sense": "min"}]}
        )
        sid_best = self._screening_candidate(store, qid, "best", 0.1)
        sid_mid = self._screening_candidate(store, qid, "mid", 0.5)
        sid_worst = self._screening_candidate(store, qid, "worst", 0.9)
        notes = compute_mod.promote_tiers(store, qid)
        assert len(notes) == 2
        promoted = {sid for sid, _tier in calls}
        assert promoted == {sid_best, sid_mid}
        assert sid_worst not in promoted
        assert all(tier == compute_mod._TIER_NEB for _sid, tier in calls)

    def test_skips_a_candidate_that_already_has_a_neb_pathway(
        self, store: Any, monkeypatch: Any
    ) -> None:
        calls = self._stub_dispatch(monkeypatch)
        qid = self._quest(store, tier_promote_neb=5, tier_promote_verify=0)
        eligible = self._screening_candidate(store, qid, "eligible", 0.3)
        already = self._screening_candidate(store, qid, "already", 0.1)
        store.insert_ref(
            kind="pathway",
            slug=f"pw-neb-{already}",
            title="pw",
            meta={"candidate_ref": already, "tier": "neb"},
            parent_id=already,
        )
        compute_mod.promote_tiers(store, qid)
        promoted = {sid for sid, _tier in calls}
        assert promoted == {eligible}
        assert already not in promoted

    def test_neb_to_verify_promotes_frontier_candidate_with_trusted_barrier(
        self, store: Any, monkeypatch: Any
    ) -> None:
        calls = self._stub_dispatch(monkeypatch)
        qid = self._quest(store, tier_promote_neb=0, tier_promote_verify=1)
        store.stamp_ref_meta(
            qid,
            {
                "rubric_objectives": [
                    {"key": "barrier", "sense": "min"},
                    {"key": "energy", "sense": "min"},
                ]
            },
        )
        # a genuine tradeoff — both land on the Pareto frontier
        lower_barrier = self._neb_frontier_candidate(
            store, qid, "low-barrier", 0.3, -5.0
        )
        self._neb_frontier_candidate(store, qid, "low-energy", 0.6, -20.0)
        notes = compute_mod.promote_tiers(store, qid)
        assert len(notes) == 1
        promoted = {sid for sid, _tier in calls}
        assert promoted == {lower_barrier}  # ranked best-first on `barrier`
        assert calls[0][1] == compute_mod._TIER_VERIFY

    def test_neb_to_verify_skips_untrusted_barrier(
        self, store: Any, monkeypatch: Any
    ) -> None:
        calls = self._stub_dispatch(monkeypatch)
        qid = self._quest(store, tier_promote_neb=0, tier_promote_verify=1)
        store.stamp_ref_meta(
            qid, {"rubric_objectives": [{"key": "barrier", "sense": "min"}]}
        )
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "untrusted", "structure": _SPEC}
        )
        assert sid is not None
        store.structure_record_run(
            sid,
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=5,
            max_disp=0.0,
            energy=-5.0,
        )
        store.stamp_ref_meta(
            sid, {"barrier": 0.2, "barrier_trusted": False, "barrier_tier": "neb"}
        )
        assert compute_mod.promote_tiers(store, qid) == []
        assert calls == []

    def test_skips_a_candidate_that_already_has_a_verify_pathway(
        self, store: Any, monkeypatch: Any
    ) -> None:
        calls = self._stub_dispatch(monkeypatch)
        qid = self._quest(store, tier_promote_neb=0, tier_promote_verify=5)
        store.stamp_ref_meta(
            qid, {"rubric_objectives": [{"key": "barrier", "sense": "min"}]}
        )
        already = self._neb_frontier_candidate(store, qid, "already", 0.2, -5.0)
        store.insert_ref(
            kind="pathway",
            slug=f"pw-verify-{already}",
            title="pw",
            meta={"candidate_ref": already, "tier": "verify"},
            parent_id=already,
        )
        compute_mod.promote_tiers(store, qid)
        assert calls == []


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

    def test_compute_step_raise_never_crashes_the_tick(
        self, store: Any, monkeypatch: Any, caplog: Any
    ) -> None:
        """gr172886: run_compute_step's dispatch lane (dispatch_autocatpath)
        raises loudly on the no-GPU null-route misconfiguration. That raise must
        NOT propagate out of run_quest_tick — otherwise it reaches the
        coordinator's blanket except and terminalizes the whole coordinator job,
        losing the mid-slice checkpoint. The tick honors its documented
        'a raise here must never crash the tick' contract: log it loudly, then
        degrade to a zero-dispatch (backed-off) outcome so the stall/escalation
        ladder takes over."""

        def _boom(*_a: Any, **_kw: Any) -> Any:
            raise RuntimeError("autocatpath dispatch: no GPU route node (gr172886)")

        monkeypatch.setattr(compute_mod, "run_compute_step", _boom)
        qid = _mk_quest(store, "A striving")
        payload = {
            "logbook": [],
            "dossier_markdown": "",
            "proposals": [{"name": "Fe", "rationale": "x", "structure": _SPEC}],
        }
        with caplog.at_level("ERROR", logger="precis.quest.tick"):
            out = run_quest_tick(
                store, qid, dispatch_fn=_fake_dispatch(payload), compute=True
            )
        # did not crash; degraded to a zero-dispatch backed-off outcome
        assert out.sims_dispatched == 0
        assert out.candidates_created == 0
        # the misconfig stayed loud in the logs (not silently swallowed)
        assert any("compute step raised" in r.getMessage() for r in caplog.records)


# ── candidate lineage — `parent` field → `derived-from` link (Slice 4c-1) ──

_CHILD_SPEC = {
    "cell": {"a": 8.4, "b": 8.4, "c": 24.0, "pbc": [True, True, False]},
    "ops": [{"op": "add_atom", "element": "Co", "frac": [0.0, 0.0, 0.5]}],
}


class TestCandidateLineage:
    def test_parent_field_creates_derived_from_link(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        parent_id = compute_mod.ensure_candidate(
            store, qid, {"name": "parent", "structure": _SPEC}
        )
        assert parent_id is not None
        parent_ref = store.fetch_refs_by_ids({parent_id})[parent_id]
        child_id = compute_mod.ensure_candidate(
            store,
            qid,
            {"name": "child", "structure": _CHILD_SPEC, "parent": parent_ref.slug},
        )
        assert child_id is not None
        links = store.links_for(child_id, direction="out", relation="derived-from")
        assert parent_id in {ln.dst_ref_id for ln in links}

    def test_unresolvable_parent_is_tolerated_not_failed(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid, was_dup, note = compute_mod._ensure_candidate_detail(
            store, qid, {"name": "x", "structure": _SPEC, "parent": "not-a-real-slug"}
        )
        assert sid is not None  # never fails the proposal
        assert was_dup is False
        assert note is not None and "lineage skipped" in note
        assert (
            store.fetch_refs_by_ids({sid})[sid].kind == "structure"
        )  # candidate still created
        # no derived-from link was written for the unresolvable parent
        links = store.links_for(sid, direction="out", relation="derived-from")
        assert links == []

    def test_self_referential_parent_is_a_silent_noop(self, store: Any) -> None:
        # A proposal's own (not-yet-existing) slug can't be its own parent in
        # practice, but a proposer echoing its own candidate back must not
        # explode or self-link.
        qid = _mk_quest(store, "A striving")
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "x", "structure": _SPEC}
        )
        assert sid is not None
        slug = store.fetch_refs_by_ids({sid})[sid].slug
        note = compute_mod._link_parent_if_present(store, qid, {"parent": slug}, sid)
        assert note is None
        assert store.links_for(sid, direction="out", relation="derived-from") == []


# ── duplicate-proposal feedback (Slice 4c-2) ────────────────────────────


class TestDuplicateProposalFeedback:
    def test_duplicate_spec_logs_observation_with_pending_status(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        slug_first = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe", "structure": _SPEC}
        )
        assert slug_first is not None
        sid2, was_dup, _note = compute_mod._ensure_candidate_detail(
            store, qid, {"name": "Fe", "structure": _SPEC}
        )
        assert sid2 == slug_first
        assert was_dup is True
        logs = [
            b for b in store.list_blocks_for_ref(qid) if b.chunk_kind == "quest_log"
        ]
        dup_logs = [b for b in logs if "duplicate proposal" in b.text]
        assert dup_logs, logs
        assert "status: pending" in dup_logs[0].text
        assert (dup_logs[0].meta or {}).get("by") == "system"

    def test_duplicate_spec_status_reflects_ruled_out_tag(self, store: Any) -> None:
        from precis.store import Tag

        qid = _mk_quest(store, "A striving")
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe", "structure": _SPEC}
        )
        assert sid is not None
        store.add_tag(sid, Tag.open("ruled-out:relax-failed"), set_by="system")
        _sid2, was_dup, _note = compute_mod._ensure_candidate_detail(
            store, qid, {"name": "Fe", "structure": _SPEC}
        )
        assert was_dup is True
        logs = [
            b for b in store.list_blocks_for_ref(qid) if b.chunk_kind == "quest_log"
        ]
        dup_logs = [b for b in logs if "duplicate proposal" in b.text]
        assert dup_logs and "status: ruled-out:relax-failed" in dup_logs[-1].text

    def test_run_compute_step_counts_and_surfaces_duplicates(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        proposals = [{"name": "Fe", "rationale": "x", "structure": _SPEC}]
        step1 = compute_mod.run_compute_step(store, qid, proposals, dispatch=False)
        assert step1.candidates_created == 1
        assert step1.duplicate_proposals == 0
        step2 = compute_mod.run_compute_step(store, qid, proposals, dispatch=False)
        assert step2.candidates_created == 1
        assert step2.duplicate_proposals == 1

    def test_run_compute_step_surfaces_unresolvable_parent_note(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        proposals = [
            {
                "name": "Fe",
                "rationale": "x",
                "structure": _SPEC,
                "parent": "not-a-real-slug",
            }
        ]
        step = compute_mod.run_compute_step(store, qid, proposals, dispatch=False)
        assert any("lineage skipped" in n for n in step.notes)


# ── geometry hash + frontier dup flag (Slice 4c-3) ──────────────────────

_GEOM_A = {
    "cell": {"a": 8.4, "b": 8.4, "c": 24.0, "pbc": [True, True, False]},
    "ops": [{"op": "add_atom", "element": "Fe", "frac": [0.0, 0.0, 0.5]}],
}
_GEOM_B = {
    # A different cell (not part of the geometry hash — only species + frac
    # positions are) so this is a distinct content-addressed candidate that
    # happens to relax the SAME atoms.
    "cell": {"a": 8.4, "b": 8.4, "c": 25.0, "pbc": [True, True, False]},
    "ops": [{"op": "add_atom", "element": "Fe", "frac": [0.0, 0.0, 0.5]}],
}


class TestGeomHash:
    def test_geom_hash_stamped_at_creation(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe", "structure": _SPEC}
        )
        assert sid is not None
        ref = store.fetch_refs_by_ids({sid})[sid]
        gh = (ref.meta or {}).get("geom_hash")
        assert isinstance(gh, str) and len(gh) == 12

    def test_identical_geometry_different_spec_shares_geom_hash(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        sid_a = compute_mod.ensure_candidate(
            store, qid, {"name": "a", "structure": _GEOM_A}
        )
        sid_b = compute_mod.ensure_candidate(
            store, qid, {"name": "b", "structure": _GEOM_B}
        )
        assert sid_a is not None and sid_b is not None and sid_a != sid_b
        refs = store.fetch_refs_by_ids({sid_a, sid_b})
        assert refs[sid_a].meta["geom_hash"] == refs[sid_b].meta["geom_hash"]


class TestFrontierGeomDuplicateFlag:
    def test_later_candidate_flagged_duplicate_of_earlier(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid_a = compute_mod.ensure_candidate(
            store, qid, {"name": "a", "structure": _GEOM_A}
        )
        sid_b = compute_mod.ensure_candidate(
            store, qid, {"name": "b", "structure": _GEOM_B}
        )
        assert sid_a is not None and sid_b is not None
        fr = quest_frontier(store, qid)
        by_id = {c.ref_id: c for c in (fr.frontier + fr.dominated + fr.unevaluated)}
        assert "duplicate_of" not in by_id[sid_a].flags
        assert by_id[sid_b].flags.get("duplicate_of") == by_id[sid_a].handle
        # flagged, not excluded — the dup still ranks (both unconverged here,
        # so both land in `unevaluated`, neither dropped).
        assert sid_b in by_id


# ── frontier-tree pinned dossier chunk (Slice 4c-4) ─────────────────────


class TestFrontierTreeDossierChunk:
    def test_creates_pinned_chunk_with_lineage_and_measure(self, store: Any) -> None:
        from precis.quest import dossier as dossier_mod

        qid = _mk_quest(store, "A striving")
        parent_id = compute_mod.ensure_candidate(
            store, qid, {"name": "parent-mat", "structure": _SPEC}
        )
        assert parent_id is not None
        parent_ref = store.fetch_refs_by_ids({parent_id})[parent_id]
        child_id = compute_mod.ensure_candidate(
            store,
            qid,
            {
                "name": "child-mat",
                "structure": _CHILD_SPEC,
                "parent": parent_ref.slug,
            },
        )
        assert child_id is not None
        store.structure_record_run(
            parent_id,
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=10,
            max_disp=0.0,
            energy=-12.5,
        )

        handle1 = dossier_mod.update_frontier_tree(store, qid)
        did, _h, _text = dossier_mod.read_dossier(store, qid)
        assert did is not None
        chunks = store.reading_order(did)
        tree_chunk = next(
            c for c in chunks if (c.meta or {}).get("pinned") == "frontier-tree"
        )
        assert tree_chunk.handle == handle1
        assert "parent-mat" in tree_chunk.text
        assert "child-mat" in tree_chunk.text
        lines = [ln for ln in tree_chunk.text.splitlines() if ln.strip()]
        parent_line = next(ln for ln in lines if "parent-mat" in ln)
        child_line = next(ln for ln in lines if "child-mat" in ln)
        assert not parent_line.startswith(" ")  # root: no indent
        assert child_line.startswith(" ")  # nested under its parent

        # regenerating (e.g. a second tick) rewrites the SAME chunk in place.
        handle2 = dossier_mod.update_frontier_tree(store, qid)
        assert handle2 == handle1

    def test_ruled_out_and_dup_markers_render(self, store: Any) -> None:
        from precis.store import Tag

        qid = _mk_quest(store, "A striving")
        sid_a = compute_mod.ensure_candidate(
            store, qid, {"name": "a", "structure": _GEOM_A}
        )
        sid_b = compute_mod.ensure_candidate(
            store, qid, {"name": "b", "structure": _GEOM_B}
        )
        assert sid_a is not None and sid_b is not None
        store.add_tag(sid_a, Tag.open("ruled-out:relax-failed"), set_by="system")

        text = render_frontier_tree(store, qid)
        assert "ruled-out:relax-failed" in text
        assert "dup-of" in text

    def test_screen_to_verify_delta_line_when_barrier_superseded(
        self, store: Any
    ) -> None:
        """Tier-ladder UX item 4: once a candidate's canonical ``barrier``
        was superseded by a verify-tier run (``barrier_tier == 'verify'`` +
        a kept ``barrier_screen`` — :func:`compute._canonicalize_barrier`'s
        own contract), the frontier-tree line shows the delta itself
        (``"screen 0.84 → verified 0.96"``) rather than a single figure."""
        qid = _mk_quest(store, "A striving")
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Pd-ladder", "structure": _SPEC}
        )
        assert sid is not None
        store.stamp_ref_meta(
            sid,
            {"barrier": 0.96, "barrier_screen": 0.84, "barrier_tier": "verify"},
        )
        text = render_frontier_tree(store, qid)
        line = next(ln for ln in text.splitlines() if "Pd-ladder" in ln)
        assert "screen 0.84 → verified 0.96" in line

    def test_survives_narrative_rewrite(self, store: Any) -> None:
        from precis.quest import dossier as dossier_mod

        qid = _mk_quest(store, "A striving")
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe", "structure": _SPEC}
        )
        assert sid is not None
        dossier_mod.update_frontier_tree(store, qid)
        did, _h, _text = dossier_mod.read_dossier(store, qid)
        assert did is not None
        before = next(
            c
            for c in store.reading_order(did)
            if (c.meta or {}).get("pinned") == "frontier-tree"
        ).text

        dossier_mod.rewrite_dossier(store, qid, "# fresh narrative\n\nsomething new")

        after_tree = next(
            c
            for c in store.reading_order(did)
            if (c.meta or {}).get("pinned") == "frontier-tree"
        ).text
        assert after_tree == before  # untouched by the whole-rewrite
        # the narrative itself carries only the model's rewrite — no pinned
        # chunk (ledger or frontier-tree) leaks into it.
        assert dossier_mod.read_narrative(store, qid) == (
            "# fresh narrative\n\nsomething new"
        )

    def test_regenerated_at_end_of_tick_after_harvest(self, store: Any) -> None:
        from precis.quest import dossier as dossier_mod

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
        did, _h, _text = dossier_mod.read_dossier(store, qid)
        assert did is not None
        tree_chunk = next(
            c
            for c in store.reading_order(did)
            if (c.meta or {}).get("pinned") == "frontier-tree"
        )
        assert "Fe" in tree_chunk.text

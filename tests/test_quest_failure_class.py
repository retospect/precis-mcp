"""Failure classes for untrusted barriers (gr593138).

:class:`precis.quest.failure_class` classifies every ``barrier_trusted:
False`` stamp — mechanical (stale engine, seed shortfall, infra/timeout
markers) vs science (endpoint split, detachment, wrong binder, absurd
magnitude, twin disagreement) — so the mechanical ones auto-requeue on the
current engine under retry/cost/cooldown limits (EXEMPT from the
``fidelity_restale_cap=0`` hold, Reto ruling 2026-11-12) and the science
ones park for the quest agent with the blocker named. Runs against real PG
(the ``store`` fixture); dispatch is monkeypatched so no GPU compute runs.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest import compute as compute_mod
from precis.quest import failure_class as fc

_SPEC = {
    "cell": {"a": 8.4, "b": 8.4, "c": 24.0, "pbc": [True, True, False]},
    "ops": [{"op": "add_atom", "element": "Fe", "frac": [0.0, 0.0, 0.5]}],
}

_REACTION = {"substrate": "NO", "target": "NH3", "network": "ammonia"}

#: ``_dispatch_landed``'s success shape — the only return that counts as a
#: mint (kept in sync with dispatch's own contract by construction here).
_LANDED_NOTE = "autocatpath[ml] dispatched 3 seed(s) for st-x + aggregate"


def _mk_quest(store: Any, text: str) -> int:
    resp = QuestHandler(hub=Hub(store=store)).put(text=text)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _mechanical_candidate(store: Any, qid: int, **over: Any) -> int:
    """A candidate carrying an untrusted mechanical barrier stamp."""
    sid = compute_mod.ensure_candidate(store, qid, {"name": "Fe", "structure": _SPEC})
    assert sid is not None
    store.stamp_ref_meta(
        qid,
        {"reaction_config": _REACTION, "fidelity_restale_cap": 0},
    )
    store.stamp_ref_meta(
        sid,
        {
            "barrier": 0.7,
            "barrier_trusted": False,
            "barrier_blocked_by": ["engine:stale"],
            "barrier_seed_blocker": "engine:stale",
            "barrier_failure_class": fc.MECHANICAL,
            **over,
        },
    )
    return sid


def _fake_dispatch(calls: list[dict[str, Any]]) -> Any:
    def _d(
        store: Any,
        structure_ref_id: int,
        config: dict[str, Any],
        **kw: Any,
    ) -> str:
        calls.append({"sid": structure_ref_id, "config": config, **kw})
        minted = kw.get("minted_by_node")
        if minted is not None:
            seeds = kw.get("seeds")
            minted["n0"] = len(seeds) if seeds else 3
        return _LANDED_NOTE

    return _d


class TestClassify:
    def test_trusted_barrier_has_no_class(self) -> None:
        assert fc.classify_untrusted({"barrier_trusted": True}) is None

    def test_absent_verdict_has_no_class(self) -> None:
        assert fc.classify_untrusted({}) is None

    @pytest.mark.parametrize(
        "meta",
        [
            {"barrier_blocked_by": ["engine:stale"]},
            {"barrier_seed_blocker": "engine:unknown"},
            {"barrier_seed_blocker": "seeds:1-independent"},
            {"barrier_seed_blocker": "seeds:unknown"},
            {"barrier_seed_blocker": "endpoint:union-not-run"},
            {"barrier_blocked_by": ["anchor:none"]},
            {"quest_infra_retries": 1},
            {"quest_autocatpath_infra_retries": 1},
            {"quest_seed_infra_retries": 1},
            {"quest_seed_timeout_noted": 123},
        ],
    )
    def test_mechanical_stamps(self, meta: dict[str, Any]) -> None:
        assert (
            fc.classify_untrusted({"barrier_trusted": False, **meta}) == fc.MECHANICAL
        )

    @pytest.mark.parametrize(
        "meta",
        [
            {"barrier_absurd": True},
            {"barrier_desorbed": 1},
            {"barrier_wrong_site": 1},
            {"barrier_twin_disagreement": "structure:42"},
            {"barrier_seed_blocker": "endpoint:union-disagree"},
            {"barrier_blocked_by": ["R->M#s0#detachment"]},
            {"barrier_blocked_by": ["R->M#s1#wrong_binder"]},
            {"barrier_blocked_by": ["R->M#s0#endpoint_agreement"]},
            {"barrier_blocked_by": ["R->M#s0#absurd_magnitude"]},
            {"barrier_blocked_by": ["no_route"]},
        ],
    )
    def test_science_stamps(self, meta: dict[str, Any]) -> None:
        assert fc.classify_untrusted({"barrier_trusted": False, **meta}) == fc.SCIENCE

    def test_science_wins_on_a_mixed_stamp(self) -> None:
        meta = {
            "barrier_trusted": False,
            "barrier_seed_blocker": "engine:stale",
            "barrier_desorbed": 1,
        }
        assert fc.classify_untrusted(meta) == fc.SCIENCE

    def test_unrecognized_untrusted_stamp_fails_toward_the_human(self) -> None:
        assert fc.classify_untrusted({"barrier_trusted": False}) == fc.SCIENCE

    def test_reasons_name_the_blockers(self) -> None:
        meta = {
            "barrier_trusted": False,
            "barrier_seed_blocker": "engine:stale",
            "barrier_absurd": True,
        }
        assert fc.untrusted_reasons(meta) == ["stale engine", "absurd magnitude"]
        assert fc.untrusted_reasons({"barrier_trusted": True}) == []


class TestHarvestStampsTheClass:
    def test_science_class_stamped_for_a_desorbed_pathway(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe", "structure": _SPEC}
        )
        assert sid is not None
        pw = store.insert_ref(
            kind="job",
            slug=None,
            title="pw",
            meta={
                "warnings": [
                    "NH3 seed=0 geometry: adsorbate atom 41 detached from slab (3.89 A)"
                ],
            },
            parent_id=sid,
        ).id
        store.insert_ref(
            kind="job",
            slug=None,
            title="autocatpath_explore",
            meta={
                "job_type": "autocatpath_explore",
                "result": {"barrier": 0.4},
                "pathway_ref": pw,
            },
            parent_id=sid,
        )
        compute_mod.harvest_measures(store, qid)
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert meta["barrier_trusted"] is False
        assert meta["barrier_failure_class"] == fc.SCIENCE
        logs = [
            b
            for b in store.chunks.list_chunks_for_ref(qid)
            if b.chunk_kind == "quest_log"
        ]
        assert any("science failure for" in b.text for b in logs)
        assert any("detachment" in b.text for b in logs)

    def test_mechanical_class_stamped_for_a_stale_pathway(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(compute_mod._AUTOCATPATH_VERSION_ENV, "0.39.0")
        qid = _mk_quest(store, "A striving")
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe", "structure": _SPEC}
        )
        assert sid is not None
        pw = store.insert_ref(
            kind="pathway",
            slug="pw-stale",
            title="pw",
            meta={
                "autocatpath_version": "0.38.0",
                "candidate_ref": sid,
                "results": {
                    "trust_schema": 1,
                    "trust_summary": {
                        "barrier": {"available": True, "blocked_by": []},
                        "selectivity": {"available": True, "blocked_by": []},
                    },
                    "trust": [],
                    "seeds": [0, 1],
                },
            },
            parent_id=sid,
        ).id
        store.insert_ref(
            kind="job",
            slug=None,
            title="autocatpath_explore",
            meta={
                "job_type": "autocatpath_explore",
                "result": {"barrier": 0.4},
                "pathway_ref": pw,
            },
            parent_id=sid,
        )
        compute_mod.harvest_measures(store, qid)
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert meta["barrier_trusted"] is False
        assert meta["barrier_failure_class"] == fc.MECHANICAL
        # no science-routing entry — the rerun arm owns the mechanical class
        logs = [
            b
            for b in store.chunks.list_chunks_for_ref(qid)
            if b.chunk_kind == "quest_log"
        ]
        assert not any("science failure for" in b.text for b in logs)


class TestStructNebSelectionStamp:
    def test_untrusted_stale_edge_stamps_the_class(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("precis.structure.neb.neb_engine_token", lambda: "neb-2")
        sel = compute_mod._neb_candidate_stamp(
            {
                "7": {
                    "fs_ref_id": 7,
                    "neb_run_id": 3,
                    "engine": "neb-1",
                    "barrier": 0.4,
                    "trusted": True,
                }
            },
            {"barrier_source": "struct_neb"},
        )
        assert sel is not None
        assert sel["barrier_trusted"] is False
        assert sel["barrier_failure_class"] == fc.MECHANICAL

    def test_trusted_edge_carries_no_class(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("precis.structure.neb.neb_engine_token", lambda: "neb-2")
        sel = compute_mod._neb_candidate_stamp(
            {
                "7": {
                    "fs_ref_id": 7,
                    "neb_run_id": 3,
                    "engine": "neb-2",
                    "barrier": 0.4,
                    "trusted": True,
                }
            },
            {},
        )
        assert sel is not None
        assert sel["barrier_trusted"] is True
        assert sel["barrier_failure_class"] is None


class TestMechanicalRerunArm:
    def test_requeues_under_the_hold_exempt(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """fidelity_restale_cap=0 holds every OTHER automatic re-mint, but
        Reto's 2026-11-12 ruling exempts mechanical reruns: the dispatch
        lands, counters and budget spend stamp, the note says why."""
        qid = _mk_quest(store, "A striving")
        sid = _mechanical_candidate(store, qid)
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_dispatch(calls))
        step = compute_mod.harvest_measures(store, qid, hub=object())
        assert [c["sid"] for c in calls] == [sid]
        assert calls[0]["created_by"] == "harvest_measures:mechanical-rerun"
        assert calls[0]["seeds"] is None  # engine-stale re-mints the fan-out
        assert any(
            "mechanical failure for" in n and "re-dispatched" in n for n in step.notes
        )
        assert any(
            "exempt from the fidelity_restale_cap=0 hold" in n for n in step.notes
        )
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert meta["barrier_mechanical_reruns"] == 1
        assert meta["barrier_mechanical_rerun_last_at"]
        # 3 default seeds × 1.5 GPU-h (the cost rule)
        qmeta = store.fetch_refs_by_ids({qid})[qid].meta or {}
        assert qmeta["barrier_mechanical_gpu_h_spent"] == 4.5

    def test_seed_shortfall_reruns_with_fresh_seed_numbers(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        qid = _mk_quest(store, "A striving")
        sid = _mechanical_candidate(
            store,
            qid,
            barrier_seed_blocker="seeds:1-independent",
            barrier_blocked_by=["seeds:1-independent"],
        )
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_dispatch(calls))
        compute_mod.harvest_measures(store, qid, hub=object())
        assert [c["sid"] for c in calls] == [sid]
        # declared (0,1,2) + two fresh numbers past the max
        assert calls[0]["seeds"] == [0, 1, 2, 3, 4]

    def test_science_class_never_requeues(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        qid = _mk_quest(store, "A striving")
        _mechanical_candidate(
            store,
            qid,
            barrier_absurd=True,
            barrier_failure_class=fc.SCIENCE,
        )
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_dispatch(calls))
        step = compute_mod.harvest_measures(store, qid, hub=object())
        assert calls == []
        assert not any("mechanical failure for" in n for n in step.notes)

    def test_retry_cap_parks_for_the_agent(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        qid = _mk_quest(store, "A striving")
        _mechanical_candidate(store, qid, barrier_mechanical_reruns=2)
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_dispatch(calls))
        step = compute_mod.harvest_measures(store, qid, hub=object())
        assert calls == []
        assert any("retry cap reached (2/2)" in n for n in step.notes)

    def test_quest_gpu_h_budget_holds_the_rerun(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        qid = _mk_quest(store, "A striving")
        sid = _mechanical_candidate(store, qid)
        store.stamp_ref_meta(qid, {"fidelity_mechanical_gpu_h_budget": 2.0})
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_dispatch(calls))
        step = compute_mod.harvest_measures(store, qid, hub=object())
        assert calls == []
        assert any("GPU-h budget" in n for n in step.notes)
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert "barrier_mechanical_reruns" not in meta

    def test_cooldown_blocks_an_immediate_second_rerun(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        qid = _mk_quest(store, "A striving")
        sid = _mechanical_candidate(store, qid)
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_dispatch(calls))
        compute_mod.harvest_measures(store, qid, hub=object())
        assert len(calls) == 1
        # the cooldown stamp just landed — a second pass must not re-dispatch
        compute_mod.harvest_measures(store, qid, hub=object())
        assert len(calls) == 1

    def test_legacy_stamp_without_class_key_requeues(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pre-existing untrusted stamp has no stored class: the arm
        classifies it from the meta rather than skipping it."""
        qid = _mk_quest(store, "A striving")
        sid = _mechanical_candidate(store, qid)
        store.stamp_ref_meta(sid, {"barrier_failure_class": None})
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_dispatch(calls))
        compute_mod.harvest_measures(store, qid, hub=object())
        assert [c["sid"] for c in calls] == [sid]

    def test_struct_neb_source_defers_to_the_stale_edge_lane(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        qid = _mk_quest(store, "A striving")
        _mechanical_candidate(store, qid, barrier_source="struct_neb")
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_dispatch(calls))
        compute_mod.harvest_measures(store, qid, hub=object())
        assert calls == []

    def test_naive_cooldown_stamp_does_not_crash(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        qid = _mk_quest(store, "A striving")
        _mechanical_candidate(
            store, qid, barrier_mechanical_rerun_last_at="2026-01-01T00:00:00"
        )
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_dispatch(calls))
        compute_mod.harvest_measures(store, qid, hub=object())
        assert len(calls) == 1

    def test_reset_clears_class_and_counters(self) -> None:
        for k in (
            "barrier_failure_class",
            "barrier_mechanical_reruns",
            "barrier_mechanical_rerun_last_at",
            "barrier_science_routed_key",
        ):
            assert k in compute_mod._AUTOCATPATH_MEASURE_KEYS

    def test_in_flight_pathway_skips_silently(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        qid = _mk_quest(store, "A striving")
        sid = _mechanical_candidate(store, qid)
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_dispatch(calls))
        monkeypatch.setattr(compute_mod, "_live_computing_pathway", lambda *_a: 99)
        step = compute_mod.harvest_measures(store, qid, hub=object())
        assert calls == []
        assert not any("mechanical failure for" in n for n in step.notes)


class TestMechanicalSpendAccounting:
    def test_reused_jobs_charge_nothing(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dispatch that dedups onto existing jobs (no new job) spends no
        retry slot and no GPU-h."""
        qid = _mk_quest(store, "A striving")
        sid = _mechanical_candidate(store, qid)

        def _reused(*_a: Any, **_kw: Any) -> str:
            return _LANDED_NOTE  # landed shape, but minted_by_node stays empty

        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _reused)
        step = compute_mod.harvest_measures(store, qid, hub=object())
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert "barrier_mechanical_reruns" not in meta
        qmeta = store.fetch_refs_by_ids({qid})[qid].meta or {}
        assert not qmeta.get("barrier_mechanical_gpu_h_spent")
        assert any("minted nothing" in n for n in step.notes)

    def test_partial_mint_charges_committed_jobs(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A later seed failing after two jobs committed still pays for them."""
        qid = _mk_quest(store, "A striving")
        sid = _mechanical_candidate(store, qid)

        def _partial(*_a: Any, **kw: Any) -> str:
            kw["minted_by_node"]["n0"] = 2
            return "autocatpath dispatch failed for st-x: tree mint (boom)"

        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _partial)
        compute_mod.harvest_measures(store, qid, hub=object())
        qmeta = store.fetch_refs_by_ids({qid})[qid].meta or {}
        assert qmeta["barrier_mechanical_gpu_h_spent"] == 3.0
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert meta["barrier_mechanical_reruns"] == 1

    def test_stale_snapshot_trusted_verdict_does_not_dispatch(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The arm reads the current stamp, not the pass-start snapshot."""
        qid = _mk_quest(store, "A striving")
        sid = _mechanical_candidate(store, qid)
        store.stamp_ref_meta(sid, {"barrier_trusted": True})
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_dispatch(calls))
        s = store.fetch_refs_by_ids({sid})[sid]
        compute_mod._mechanical_rerun_arm(
            store,
            qid,
            s,
            handle="st-x",
            hub=object(),
            rerun_held=True,
            notes=[],
        )
        assert calls == []

    def test_job_failure_class_gate(self) -> None:
        assert compute_mod._mechanical_job_failure({"failure_class": "timeout"})
        assert compute_mod._mechanical_job_failure({"failure_class": "infra"})


class TestMechanicalBudgetFanOut:
    """The GPU-h budget pre-check must estimate the dispatch's EFFECTIVE
    fan-out — models × tier-widened seeds — not the declared seed count,
    and a zero budget must hold, not bypass (gr593138 review, finding 1)."""

    def _reaction_candidate(
        self, store: Any, qid: int, reaction: dict[str, Any], **over: Any
    ) -> int:
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe", "structure": _SPEC}
        )
        assert sid is not None
        store.stamp_ref_meta(
            qid, {"reaction_config": reaction, "fidelity_restale_cap": 0}
        )
        store.stamp_ref_meta(
            sid,
            {
                "barrier": 0.7,
                "barrier_trusted": False,
                "barrier_blocked_by": ["engine:stale"],
                "barrier_seed_blocker": "engine:stale",
                "barrier_failure_class": fc.MECHANICAL,
                **over,
            },
        )
        return sid

    def test_neb_widening_counted_in_the_estimate(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Declared seeds [0] dispatch as the widened [0,1,2] fan-out: the
        estimate is 3 seeds × 1.5 = 4.5 GPU-h, so a 3.0 budget holds. The
        declared-count estimate (1.5) would have dispatched past it."""
        qid = _mk_quest(store, "A striving")
        reaction = {**_REACTION, "search": {"seeds": [0]}}
        self._reaction_candidate(store, qid, reaction)
        store.stamp_ref_meta(qid, {"fidelity_mechanical_gpu_h_budget": 3.0})
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_dispatch(calls))
        step = compute_mod.harvest_measures(store, qid, hub=object())
        assert calls == []
        assert any("GPU-h budget" in n for n in step.notes)

    def test_verify_widening_counted_in_the_estimate(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A verify-rung candidate re-dispatches at verify, whose overlay
        widens a short declared list to the 3-seed spread — same 4.5 GPU-h
        estimate against the same 3.0 budget."""
        qid = _mk_quest(store, "A striving")
        reaction = {**_REACTION, "search": {"seeds": [0]}}
        self._reaction_candidate(store, qid, reaction, barrier_fidelity="verify")
        store.stamp_ref_meta(qid, {"fidelity_mechanical_gpu_h_budget": 3.0})
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_dispatch(calls))
        compute_mod.harvest_measures(store, qid, hub=object())
        assert calls == []

    def test_multi_model_config_counted_in_the_estimate(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two MLIP models mint one job per (model, seed): the estimate is
        2 × 3 × 1.5 = 9 GPU-h, so a 6.0 budget holds. The seed-count-only
        estimate (4.5) would have dispatched."""
        qid = _mk_quest(store, "A striving")
        reaction = {**_REACTION, "mlip": {"models": ["mace:a", "mace:b"]}}
        self._reaction_candidate(store, qid, reaction)
        store.stamp_ref_meta(qid, {"fidelity_mechanical_gpu_h_budget": 6.0})
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_dispatch(calls))
        step = compute_mod.harvest_measures(store, qid, hub=object())
        assert calls == []
        assert any("GPU-h budget" in n for n in step.notes)

    def test_zero_budget_holds_every_rerun(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A zero budget is a budget, not a bypass: any positive estimate
        holds the rerun."""
        qid = _mk_quest(store, "A striving")
        self._reaction_candidate(store, qid, dict(_REACTION))
        store.stamp_ref_meta(qid, {"fidelity_mechanical_gpu_h_budget": 0.0})
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_dispatch(calls))
        step = compute_mod.harvest_measures(store, qid, hub=object())
        assert calls == []
        assert any("GPU-h budget" in n and "0 exceeded" in n for n in step.notes)

    def test_estimate_exactly_at_budget_dispatches(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive control: the widened 4.5 GPU-h estimate fits a 4.5
        budget exactly — the rerun lands and charges the committed jobs."""
        qid = _mk_quest(store, "A striving")
        reaction = {**_REACTION, "search": {"seeds": [0]}}
        sid = self._reaction_candidate(store, qid, reaction)
        store.stamp_ref_meta(qid, {"fidelity_mechanical_gpu_h_budget": 4.5})
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_dispatch(calls))
        compute_mod.harvest_measures(store, qid, hub=object())
        assert [c["sid"] for c in calls] == [sid]
        qmeta = store.fetch_refs_by_ids({qid})[qid].meta or {}
        assert qmeta["barrier_mechanical_gpu_h_spent"] == 4.5


class TestFailedSeedHoldCarriesThrough:
    """A same-wall timeout or invalid-geometry refusal from the failed-seed
    branch must carry through to the failure-class rerun arm — an older
    mechanical stamp would otherwise re-dispatch the fan-out straight past
    the hold (gr593138 review, finding 2)."""

    def _stuck_seed(self, store: Any, sid: int, job_meta: dict[str, Any]) -> None:
        from precis.store import Tag

        agg_todo = store.insert_ref(
            kind="todo",
            slug=None,
            title="autocatpath aggregate",
            meta={"executor": "ssh_node", "job_type": "autocatpath_aggregate"},
            parent_id=sid,
        )
        seed_todo = store.insert_ref(
            kind="todo",
            slug=None,
            title="autocatpath seed",
            meta={"auto_check": {"type": "child_job_succeeded"}},
            parent_id=agg_todo.id,
        )
        job = store.insert_ref(
            kind="job",
            slug=None,
            title="autocatpath_seed",
            meta={"job_type": "autocatpath_seed", **job_meta},
            parent_id=seed_todo.id,
        )
        store.add_tag(job.id, Tag.closed("STATUS", "failed"), set_by="system")

    def test_same_wall_timeout_hold_suppresses_the_rerun_arm(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        qid = _mk_quest(store, "A striving")
        sid = _mechanical_candidate(store, qid)  # older mechanical stamp + hold
        wall = compute_mod._autocatpath_wall_seconds("neb")
        self._stuck_seed(
            store,
            sid,
            {
                "failure_class": "timeout",
                "params": {"resources": {"wall_seconds": wall}},
            },
        )
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_dispatch(calls))
        step = compute_mod.harvest_measures(store, qid, hub=object())
        assert calls == []
        assert any("held until the wall is raised" in n for n in step.notes)
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert "barrier_mechanical_reruns" not in meta  # no slot spent either

    def test_geometry_hold_suppresses_the_rerun_arm(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        qid = _mk_quest(store, "A striving")
        sid = _mechanical_candidate(store, qid)
        self._stuck_seed(
            store,
            sid,
            {
                "failure_class": "invalid-geometry",
                "error": "ValueError: placement puts H inside the slab",
            },
        )
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_dispatch(calls))
        step = compute_mod.harvest_measures(store, qid, hub=object())
        assert calls == []
        assert any("re-dispatch held: fidelity_restale_cap=0" in n for n in step.notes)

    def test_held_seed_suppresses_only_the_held_candidate(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The carry-through is per-candidate: a neighbour mechanical stamp
        still re-queues."""
        qid = _mk_quest(store, "A striving")
        held_sid = _mechanical_candidate(store, qid)
        spec_b = {
            "cell": {"a": 8.4, "b": 8.4, "c": 24.0, "pbc": [True, True, False]},
            "ops": [{"op": "add_atom", "element": "Fe", "frac": [0.0, 0.0, 0.62]}],
        }
        free_sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe", "structure": spec_b}
        )
        assert free_sid is not None and free_sid != held_sid
        store.stamp_ref_meta(
            free_sid,
            {
                "barrier": 0.7,
                "barrier_trusted": False,
                "barrier_blocked_by": ["engine:stale"],
                "barrier_seed_blocker": "engine:stale",
                "barrier_failure_class": fc.MECHANICAL,
            },
        )
        wall = compute_mod._autocatpath_wall_seconds("neb")
        self._stuck_seed(
            store,
            held_sid,
            {
                "failure_class": "timeout",
                "params": {"resources": {"wall_seconds": wall}},
            },
        )
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_dispatch(calls))
        compute_mod.harvest_measures(store, qid, hub=object())
        assert [c["sid"] for c in calls] == [free_sid]

    @pytest.mark.parametrize(
        "job_meta",
        [
            {"job_type": "autocatpath_seed", "failure_class": "infra"},
            {"job_type": "autocatpath_seed"},
            {"job_type": "autocatpath_aggregate", "failure_class": "infra"},
            {"job_type": "autocatpath_aggregate"},
            {"job_type": "autocatpath_explore"},
            {
                "job_type": "autocatpath_aggregate",
                "failure_class": "infra",
                "error": "unclaimable: pinned to target_node=maple, which has left the lane",
            },
        ],
        ids=[
            "seed",
            "legacy-seed",
            "aggregate",
            "legacy-aggregate",
            "explore",
            "off-lane",
        ],
    )
    def test_fresh_science_stamp_vetoes_failed_job_retries(
        self, store: Any, monkeypatch: pytest.MonkeyPatch, job_meta: dict[str, Any]
    ) -> None:
        qid = _mk_quest(store, "A striving")
        sid = _mechanical_candidate(store, qid)
        store.stamp_ref_meta(qid, {"fidelity_restale_cap": 1})
        calls: list[dict[str, Any]] = []

        def _failed_job(_store: Any, candidate_id: int) -> tuple[str, dict[str, Any]]:
            assert candidate_id == sid
            store.stamp_ref_meta(
                sid, {"barrier_absurd": True, "barrier_failure_class": fc.SCIENCE}
            )
            return "failed", job_meta

        monkeypatch.setattr(compute_mod, "_stuck_seed_failure", _failed_job)
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_dispatch(calls))
        step = compute_mod.harvest_measures(store, qid, hub=object())
        assert calls == []
        assert any("vetoed" in n and "science-class" in n for n in step.notes)
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert meta["barrier_science_routed_key"]
        assert "barrier_mechanical_reruns" not in meta
        qmeta = store.fetch_refs_by_ids({qid})[qid].meta or {}
        assert not qmeta.get("barrier_mechanical_gpu_h_spent")

    @pytest.mark.parametrize("job_type", ["autocatpath_seed", "autocatpath_aggregate"])
    @pytest.mark.parametrize("limit", ["budget", "retry-cap", "cooldown", "none"])
    def test_unheld_mechanical_retries_use_shared_limits(
        self,
        store: Any,
        monkeypatch: pytest.MonkeyPatch,
        job_type: str,
        limit: str,
    ) -> None:
        qid = _mk_quest(store, "A striving")
        sid = _mechanical_candidate(store, qid)
        store.stamp_ref_meta(qid, {"fidelity_restale_cap": 1})
        if limit == "budget":
            store.stamp_ref_meta(qid, {"fidelity_mechanical_gpu_h_budget": 0})
        elif limit == "retry-cap":
            store.stamp_ref_meta(qid, {"fidelity_mechanical_retry_cap": 0})
        elif limit == "cooldown":
            store.stamp_ref_meta(
                sid, {"barrier_mechanical_rerun_last_at": datetime.now(UTC).isoformat()}
            )
        failed_job = ("failed", {"job_type": job_type, "failure_class": "infra"})
        if job_type == "autocatpath_seed":
            self._stuck_seed(store, sid, failed_job[1])
        else:
            monkeypatch.setattr(
                compute_mod, "_latest_autocatpath_job", lambda *_: failed_job
            )
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_dispatch(calls))
        compute_mod.harvest_measures(store, qid, hub=object())
        expected = int(limit == "none")
        assert len(calls) == expected
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        qmeta = store.fetch_refs_by_ids({qid})[qid].meta or {}
        assert meta.get("barrier_mechanical_reruns", 0) == expected
        assert qmeta.get("barrier_mechanical_gpu_h_spent", 0) == expected * 4.5

    def test_legacy_science_stamp_vetoes_the_crashed_seed_retry(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same veto for a stamp with no stored class key: the class is
        derived from the fresh meta (a science flag beats the mechanical
        blocker ids)."""
        qid = _mk_quest(store, "A striving")
        sid = _mechanical_candidate(store, qid, barrier_absurd=True)
        store.stamp_ref_meta(sid, {"barrier_failure_class": None})
        self._stuck_seed(store, sid, {"failure_class": "infra"})
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_dispatch(calls))
        compute_mod.harvest_measures(store, qid, hub=object())
        assert calls == []


class TestTwinDemotionReclasses:
    """The twin-disagreement demotion must stamp ``barrier_failure_class =
    "science"`` on BOTH twins — a same-pass mechanical stamp would otherwise
    survive and the rerun arm would dispatch past the science blocker
    (gr593138 review, finding 3)."""

    _SPEC_B = {
        "cell": {"a": 8.4, "b": 8.4, "c": 24.0, "pbc": [True, True, False]},
        "ops": [{"op": "add_atom", "element": "Fe", "frac": [0.0, 0.0, 0.62]}],
    }

    def _twins(self, store: Any, qid: int) -> tuple[int, int]:
        sid_a = compute_mod.ensure_candidate(
            store, qid, {"name": "a", "structure": _SPEC}
        )
        sid_b = compute_mod.ensure_candidate(
            store, qid, {"name": "b", "structure": self._SPEC_B}
        )
        assert sid_a is not None and sid_b is not None
        assert sid_a != sid_b  # content-addressed: the specs must differ
        for sid, barrier in ((sid_a, 0.5), (sid_b, 4.99)):
            store.stamp_ref_meta(
                sid,
                {
                    "geom_hash_c": "twin-hash",
                    "barrier": barrier,
                    "barrier_trusted": True,
                    "barrier_fidelity": "neb",
                    # a barrier stamped mechanical earlier in the pass — the
                    # demotion must overwrite it
                    "barrier_failure_class": fc.MECHANICAL,
                },
            )
        return sid_a, sid_b

    def test_demotion_stamps_science_on_both_refs(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid_a, sid_b = self._twins(store, qid)
        refs = store.fetch_refs_by_ids({sid_a, sid_b})
        s_a = refs[sid_a]
        compute_mod._flag_barrier_twin_disagreement(
            store, qid, [refs[sid_a], refs[sid_b]], s_a, dict(s_a.meta or {}), "st-a"
        )
        meta_a = store.fetch_refs_by_ids({sid_a})[sid_a].meta or {}
        meta_b = store.fetch_refs_by_ids({sid_b})[sid_b].meta or {}
        assert meta_a["barrier_trusted"] is False
        assert meta_b["barrier_trusted"] is False
        assert meta_a["barrier_failure_class"] == fc.SCIENCE
        assert meta_b["barrier_failure_class"] == fc.SCIENCE

    def test_twin_demotion_blocks_the_mechanical_rerun_arm(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A's fresh barrier classifies mechanical at stamp time (stale
        engine), then the twin guard demotes it to science in the same pass:
        the rerun arm must NOT dispatch, and the stamp routes to the agent."""
        monkeypatch.setenv(compute_mod._AUTOCATPATH_VERSION_ENV, "0.39.0")
        qid = _mk_quest(store, "A striving")
        store.stamp_ref_meta(
            qid, {"reaction_config": _REACTION, "fidelity_restale_cap": 0}
        )
        sid_a = compute_mod.ensure_candidate(
            store, qid, {"name": "a", "structure": _SPEC}
        )
        sid_b = compute_mod.ensure_candidate(
            store, qid, {"name": "b", "structure": self._SPEC_B}
        )
        assert sid_a is not None and sid_b is not None
        assert sid_a != sid_b  # content-addressed: the specs must differ
        store.stamp_ref_meta(sid_a, {"geom_hash_c": "twin-hash"})
        store.stamp_ref_meta(
            sid_b,
            {
                "geom_hash_c": "twin-hash",
                "barrier": 4.0,
                "barrier_trusted": True,
                "barrier_fidelity": "neb",
            },
        )
        pw = store.insert_ref(
            kind="pathway",
            slug="pw-stale",
            title="pw",
            meta={
                "autocatpath_version": "0.38.0",
                "candidate_ref": sid_a,
                "results": {
                    "trust_schema": 1,
                    "trust_summary": {
                        "barrier": {"available": True, "blocked_by": []},
                        "selectivity": {"available": True, "blocked_by": []},
                    },
                    "trust": [],
                    "seeds": [0, 1],
                },
            },
            parent_id=sid_a,
        ).id
        store.insert_ref(
            kind="job",
            slug=None,
            title="autocatpath_explore",
            meta={
                "job_type": "autocatpath_explore",
                "result": {"barrier": 0.4},
                "pathway_ref": pw,
            },
            parent_id=sid_a,
        )
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(compute_mod, "dispatch_autocatpath", _fake_dispatch(calls))
        compute_mod.harvest_measures(store, qid, hub=object())
        assert calls == []  # the science demotion beats the stale-mechanical stamp
        meta_a = store.fetch_refs_by_ids({sid_a})[sid_a].meta or {}
        assert meta_a["barrier_failure_class"] == fc.SCIENCE
        logs = [
            b
            for b in store.chunks.list_chunks_for_ref(qid)
            if b.chunk_kind == "quest_log"
        ]
        assert any("science failure for" in b.text for b in logs)


class TestScienceRoutingDedup:
    """Every science-class stamp — fresh, legacy (no stored class key) or
    the struct_neb selection's — routes to the quest agent through ONE
    deduplicated logbook entry naming the blocker (gr593138 review,
    finding 4)."""

    def _routed_entries(self, store: Any, qid: int) -> list[Any]:
        return [
            b
            for b in store.chunks.list_chunks_for_ref(qid)
            if b.chunk_kind == "quest_log" and "science failure for" in b.text
        ]

    def test_legacy_science_stamp_routes_and_dedups(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid = _mechanical_candidate(store, qid, barrier_absurd=True)
        # legacy: the class key predates the feature — classify from meta
        store.stamp_ref_meta(sid, {"barrier_failure_class": None})
        compute_mod.harvest_measures(store, qid)
        routed = self._routed_entries(store, qid)
        assert len(routed) == 1
        assert "absurd magnitude" in routed[0].text
        meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
        assert meta["barrier_science_routed_key"]
        # a rerun without a re-measure adds nothing
        compute_mod.harvest_measures(store, qid)
        assert len(self._routed_entries(store, qid)) == 1

    def test_changed_stamp_routes_again(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid = _mechanical_candidate(store, qid, barrier_absurd=True)
        store.stamp_ref_meta(sid, {"barrier_failure_class": None})
        compute_mod.harvest_measures(store, qid)
        # a re-measure lands a different value — the new stamp routes fresh
        store.stamp_ref_meta(sid, {"barrier": 9.9})
        compute_mod.harvest_measures(store, qid)
        assert len(self._routed_entries(store, qid)) == 2

    def test_struct_neb_science_selection_routes(self, store: Any) -> None:
        """The struct_neb selection's science stamp never reaches the rerun
        arm (it defers to the stale-edge lane) — it must still reach the
        agent through the logbook."""
        qid = _mk_quest(store, "A striving")
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe", "structure": _SPEC}
        )
        assert sid is not None
        store.stamp_ref_meta(
            sid,
            {
                "barrier": 0.4,
                "barrier_trusted": False,
                "barrier_source": "struct_neb",
                "barrier_seed_blocker": "endpoint:union-disagree",
                "barrier_blocked_by": ["endpoint:union-disagree"],
            },
        )
        compute_mod.harvest_measures(store, qid)  # hub=None — note-only pass
        routed = self._routed_entries(store, qid)
        assert len(routed) == 1
        assert "endpoint split" in routed[0].text
        compute_mod.harvest_measures(store, qid)
        assert len(self._routed_entries(store, qid)) == 1
        assert not compute_mod._mechanical_job_failure({"failure_class": "science"})

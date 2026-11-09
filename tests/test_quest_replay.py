"""Tests for the quest replay benchmark — gr608711.

``precis.quest.replay`` walks a quest's serves-graph results in measurement
order and reports the optimisation numbers the proposer debate needs:
per-axis top-5% coverage + hit rate of the LLM's picks vs a seeded random
baseline, best-so-far/regret, duplicate rate, frontier coverage. Runs
against real PG (the ``store`` fixture); no GPU — candidates are minted
through :func:`precis.quest.compute.ensure_candidate` and "measured" by
``structure_record_run``.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.handlers.structure import StructureHandler
from precis.quest import compute as compute_mod
from precis.quest.replay import quest_replay, render_replay


def _mk_quest(store: Any, text: str = "A striving") -> int:
    resp = QuestHandler(hub=Hub(store=store)).put(text=text)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _mk_candidate(store: Any, qid: int, i: int) -> int:
    """One LLM pick: a distinct spec per i (content-addressed candidates),
    so each call mints a fresh structure + its lineage logbook entries."""
    spec = {
        "cell": {"a": 8.4, "b": 8.4, "c": 24.0, "pbc": [True, True, False]},
        "ops": [
            {"op": "add_atom", "element": "Fe", "frac": [0.0, 0.0, 0.5]},
            {"op": "add_atom", "element": "Co", "frac": [0.5, 0.5, 0.4 + i * 0.01]},
        ],
    }
    sid = compute_mod.ensure_candidate(
        store, qid, {"name": f"cand-{i}", "structure": spec}
    )
    assert sid is not None
    return int(sid)


def _mk_seed_structure(store: Any, qid: int, slug: str) -> int:
    """A serving structure with NO proposal lineage — the human-seed /
    struct_search case: pool, but never an LLM pick."""
    StructureHandler(hub=Hub(store=store)).put(
        id=slug,
        text=json.dumps(
            {
                "cell": {"a": 8.4, "b": 8.4, "c": 24.0, "pbc": [True, True, False]},
                "ops": [{"op": "add_atom", "element": "Ni", "frac": [0.0, 0.0, 0.5]}],
            }
        ),
    )
    ref = store.get_ref(kind="structure", id=slug)
    assert ref is not None
    store.add_link(src_ref_id=int(ref.id), dst_ref_id=qid, relation="serves")
    return int(ref.id)


def _run(store: Any, sid: int, energy: float) -> int:
    return int(
        store.structure_record_run(
            sid,
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=5,
            max_disp=0.0,
            energy=energy,
        )
    )


def _backdate_run(store: Any, run_id: int, days_ago: float) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE struct_runs SET created_at=%s WHERE id=%s",
            (datetime.now(UTC) - timedelta(days=days_ago), run_id),
        )
        conn.commit()


class TestReplayCounts:
    def test_duplicate_and_rejected_proposals_counted(self, store: Any) -> None:
        qid = _mk_quest(store)
        sid = _mk_candidate(store, qid, 0)
        _run(store, sid, -5.0)
        # same spec again → `duplicate proposal:` observation, no new candidate
        spec_dup = {
            "cell": {"a": 8.4, "b": 8.4, "c": 24.0, "pbc": [True, True, False]},
            "ops": [
                {"op": "add_atom", "element": "Fe", "frac": [0.0, 0.0, 0.5]},
                {"op": "add_atom", "element": "Co", "frac": [0.5, 0.5, 0.4]},
            ],
        }
        compute_mod.ensure_candidate(
            store, qid, {"name": "again", "structure": spec_dup}
        )
        # no structure spec → `rejected proposal:` observation
        compute_mod.ensure_candidate(store, qid, {"name": "no spec"})

        report = quest_replay(store, qid, trials=5)
        assert report is not None
        assert report.n_duplicates == 1
        assert report.n_rejected == 1
        assert report.n_proposals == 1  # only the minted candidate's hypothesis
        assert report.n_picks == 1
        assert report.pick_order == "lineage"

    def test_empty_quest_renders_clean(self, store: Any) -> None:
        qid = _mk_quest(store)
        report = quest_replay(store, qid, trials=5)
        assert report is not None
        assert report.axes == []
        text = render_replay(report)
        assert "nothing to replay" in text

    def test_missing_quest_returns_none(self, store: Any) -> None:
        assert quest_replay(store, 999999999, trials=5) is None


class TestReplayAxes:
    def test_top5_coverage_hit_rate_and_first_hit(self, store: Any) -> None:
        qid = _mk_quest(store)
        runs = []
        for i in range(20):
            sid = _mk_candidate(store, qid, i)
            # candidate 0 holds the best (lowest) energy
            runs.append((i, sid, _run(store, sid, energy=-20.0 + i)))
        # measurement order: candidate 0 measured LAST (youngest ts)
        for i, _sid, run_id in runs:
            _backdate_run(store, run_id, days_ago=i + 1)

        report = quest_replay(store, qid, trials=40, seed=3)
        assert report is not None
        ax = report.axes[0]
        assert (ax.key, ax.sense) == ("energy", "min")
        assert ax.pool == 20 and ax.top_n == 1  # 5% of 20 → 1
        assert ax.threshold == -20.0
        assert ax.llm_evaluated == 20
        assert ax.llm_hits == 1
        assert ax.llm_hit_rate == 1 / 20
        assert ax.llm_coverage == 1.0
        # the best candidate was measured last → first hit at pick #20
        assert ax.llm_first_hit == 20
        assert ax.final_best == -20.0
        # random draws the same 20 of 20 → coverage is degenerate at 1.0
        assert ax.random_coverage_mean == 1.0
        # best-so-far ends at the final best; regret zero
        assert ax.llm_curve[-1] == -20.0

    def test_unattributed_pool_member_is_not_a_pick(self, store: Any) -> None:
        qid = _mk_quest(store)
        best = _mk_seed_structure(store, qid, "seed-best")
        _run(store, best, -99.0)
        for i in range(10):
            _run(store, _mk_candidate(store, qid, i), energy=-1.0 - i)

        report = quest_replay(store, qid, trials=40, seed=3)
        assert report is not None
        ax = report.axes[0]
        assert ax.pool == 11
        assert report.n_unattributed == 1
        assert report.n_picks == 10
        # the top-5% member is the seed — the LLM never picked it
        assert ax.llm_hits == 0
        assert ax.llm_coverage == 0.0
        assert ax.llm_first_hit is None
        # the LLM's best-so-far never reaches the seed's -99
        assert ax.llm_curve[-1] is not None and ax.llm_curve[-1] > -99.0
        # random draws 10 of 11 → expected coverage 10/11 ≈ 0.91
        assert 0.6 <= ax.random_coverage_mean <= 1.0

    def test_deterministic_under_seed(self, store: Any) -> None:
        qid = _mk_quest(store)
        for i in range(8):
            _run(store, _mk_candidate(store, qid, i), energy=-float(i))
        a = quest_replay(store, qid, trials=30, seed=11)
        b = quest_replay(store, qid, trials=30, seed=11)
        assert a is not None and b is not None
        assert a.axes[0].random_hits_mean == b.axes[0].random_hits_mean
        assert a.axes[0].random_curve_mean == b.axes[0].random_curve_mean

    def test_rubric_objectives_drive_axes(self, store: Any) -> None:
        qid = _mk_quest(store)
        store.stamp_ref_meta(
            qid,
            {
                "rubric_objectives": [
                    {"key": "energy", "sense": "min"},
                    {"key": "margin", "sense": "max"},
                ]
            },
        )
        for i in range(10):
            sid = _mk_candidate(store, qid, i)
            _run(store, sid, energy=-float(i))
            store.stamp_ref_meta(sid, {"margin": float(i) * 0.1})
        report = quest_replay(store, qid, trials=10, seed=1)
        assert report is not None
        assert [(a.key, a.sense) for a in report.axes] == [
            ("energy", "min"),
            ("margin", "max"),
        ]

    def test_result_entries_supply_measurement_order(self, store: Any) -> None:
        """The logbook ``result`` citation is evidence even without a run
        timestamp — a candidate first cited earlier replays earlier."""
        qid = _mk_quest(store)
        a = _mk_candidate(store, qid, 0)
        b = _mk_candidate(store, qid, 1)
        # energy comes from meta only (no struct_runs) — e.g. a stamped axis
        store.stamp_ref_meta(a, {"energy": -1.0})
        store.stamp_ref_meta(b, {"energy": -2.0})
        # cite b first, a second — b is the better AND earlier measurement
        from precis.quest.logbook import MEASURED_BY, append_entry

        append_entry(
            store,
            qid,
            text=f"relax result for [st{b}] (cand-1): E=-2 eV, converged",
            entry_type="result",
            by=MEASURED_BY,
        )
        append_entry(
            store,
            qid,
            text=f"relax result for [st{a}] (cand-0): E=-1 eV, converged",
            entry_type="result",
            by=MEASURED_BY,
        )
        report = quest_replay(store, qid, trials=5)
        assert report is not None
        ax = report.axes[0]
        assert ax.llm_evaluated == 2
        # b (energy -2) is the top-5% member and was measured first
        assert ax.llm_first_hit == 1
        assert ax.llm_curve[-1] == -2.0


class TestReplayRender:
    def test_render_names_both_proposers(self, store: Any) -> None:
        qid = _mk_quest(store)
        for i in range(20):
            _run(store, _mk_candidate(store, qid, i), energy=-20.0 + i)
        report = quest_replay(store, qid, trials=10, seed=1)
        assert report is not None
        text = render_replay(report)
        assert f"qu{qid} replay" in text
        assert "top 5% = 1 of 20" in text
        assert "llm    picks=20" in text
        assert "random draws=20" in text
        assert "invalid-or-duplicate" in text
        assert "best-so-far" in text

    def test_frontier_coverage_reported(self, store: Any) -> None:
        qid = _mk_quest(store)
        for i in range(5):
            _run(store, _mk_candidate(store, qid, i), energy=-float(i + 1))
        report = quest_replay(store, qid, trials=10, seed=1)
        assert report is not None
        assert report.frontier_total >= 1
        assert report.frontier_captured == report.frontier_total
        text = render_replay(report)
        assert "frontier (current confirmed)" in text


class TestReplayView:
    def test_get_view_replay_matches_cli(self, store: Any) -> None:
        qid = _mk_quest(store)
        for i in range(10):
            _run(store, _mk_candidate(store, qid, i), energy=-float(i))
        h = QuestHandler(hub=Hub(store=store))
        resp = h.get(id=qid, view="replay")
        assert "llm" in resp.body and "random" in resp.body
        assert "top 5% = 1 of 10" in resp.body

    def test_replay_args_validate(self, store: Any) -> None:
        import pytest

        from precis.errors import BadInput

        qid = _mk_quest(store)
        _run(store, _mk_candidate(store, qid, 0), energy=-1.0)
        h = QuestHandler(hub=Hub(store=store))
        with pytest.raises(BadInput):
            h.get(id=qid, view="replay", args={"trials": "lots"})
        with pytest.raises(BadInput):
            h.get(id=qid, view="replay", args={"top_frac": 9})
        # valid args land
        resp = h.get(id=qid, view="replay", args={"trials": 3, "seed": 9})
        assert "mean of 3 seeded orderings" in resp.body

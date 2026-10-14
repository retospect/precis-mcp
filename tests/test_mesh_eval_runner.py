"""Mesh-eval runner — lane scoring, judge acceptance, the two bars."""

from __future__ import annotations

from typing import Any

import pytest

from precis.mesh_eval.runner import (
    CallResult,
    JudgeVerdict,
    LaneReport,
    render_report,
    run_set,
)
from precis.mesh_eval.sets import EvalSet, MeshItem


def _item(i: int, lane: str = "merge", gold: Any = None) -> MeshItem:
    return MeshItem(
        item_id=f"i-{i}", lane=lane, prompt="p", gold=gold or {"same": True}
    )


# ── runner ────────────────────────────────────────────────────────


def test_run_set_scores_and_reports_errors() -> None:
    items = [
        _item(1, gold={"same": True}),
        _item(2, gold={"same": True}),
        _item(3, gold={"same": False}),
    ]
    # One right verdict, one wrong, one unparseable → accuracy 1/3.
    replies = iter(['{"same": true}', '{"same": false}', ""])

    def call(messages: Any, mt: Any) -> CallResult:
        return CallResult(text=next(replies))

    es = EvalSet(meta={"lane": "merge"}, items=items)
    rep = run_set(es, call, model="stub")
    assert rep.n == 3 and rep.errors == 0
    assert rep.accuracy == pytest.approx(1 / 3)

    def boom(messages: Any, mt: Any) -> CallResult:
        return CallResult(text="", error="connection refused")

    rep2 = run_set(es, boom, model="stub")
    assert rep2.errors == 3 and rep2.accuracy is None


def test_run_set_judge_acceptance() -> None:
    items = [_item(i) for i in range(4)]

    es = EvalSet(meta={"lane": "merge"}, items=items)

    def call(messages: Any, mt: Any) -> CallResult:
        return CallResult(text='{"same": true}')

    def judge(item: MeshItem, text: str) -> JudgeVerdict:
        return JudgeVerdict(
            accepted=item.item_id != "i-3", input_tokens=10, output_tokens=5
        )

    rep = run_set(es, call, model="stub", judge_fn=judge)
    assert rep.n_judged == 4 and rep.n_accepted == 3
    assert rep.acceptance_rate == pytest.approx(0.75)
    assert rep.judge_tokens == 60
    assert rep.reviewer_cost_per_accepted == 20.0


def test_render_report_bars() -> None:

    rep = LaneReport(
        lane="categorise",
        model="m",
        n=10,
        errors=0,
        accuracy=0.8,
        ci_lo=0.5,
        ci_hi=0.95,
        per_item=[],
    )
    txt = render_report([rep], references={"categorise": {"frontier_retest": 0.5}})
    assert "auto-apply bar: PASS" in txt
    rep2 = LaneReport(
        lane="merge",
        model="m",
        n=10,
        errors=0,
        accuracy=0.8,
        ci_lo=0.5,
        ci_hi=0.95,
        per_item=[],
    )
    txt2 = render_report([rep2])
    assert "never auto-applies" in txt2

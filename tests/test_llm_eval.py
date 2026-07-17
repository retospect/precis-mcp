"""LLM golden-eval harness (slice 11).

Pure scorer + gold-loader tests run without a DB; the harness tests inject a
stub ``dispatch_fn`` so no real model runs. The record path uses a real card
(``upsert_card``) against real PG to prove ``run_eval`` writes measured-eval
ordinals through the catalog's existing write surface.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from precis import llm_catalog
from precis.errors import BadInput
from precis.llm_eval import run_eval
from precis.llm_eval.harness import compare
from precis.llm_eval.scorers import bucket_to_ordinal, score_needle, score_tool_json
from precis.llm_eval.tasks import GoldTask, load_gold_set

# ── scorers (pure) ────────────────────────────────────────────────


def test_needle_matches_whitespace_insensitively() -> None:
    assert score_needle("The code is  WX-4417 .", None, {"needle": "WX-4417"}) == 1.0
    assert score_needle("no idea", None, {"needle": "WX-4417"}) == 0.0


def test_needle_accepts_aliases() -> None:
    expect = {"needle": "2026-09-14", "aliases": ["Sept 14 2026"]}
    assert score_needle("it's Sept 14 2026", None, expect) == 1.0


def test_tool_json_fraction_of_keys() -> None:
    expect = {"answer": {"sku": "BOLT-88", "qty": "3", "status": "shipped"}}
    got = {"sku": "BOLT-88", "qty": 3, "status": "shipped"}  # qty int coerces
    assert score_tool_json("", got, expect) == 1.0
    partial = {"sku": "BOLT-88", "qty": 3, "status": "pending"}
    assert abs(score_tool_json("", partial, expect) - 2 / 3) < 1e-9


def test_tool_json_no_structured_output_scores_zero() -> None:
    expect = {"answer": {"sku": "BOLT-88"}}
    assert score_tool_json("BOLT-88 in prose", None, expect) == 0.0


def test_bucket_maps_0_to_1_and_1_to_5() -> None:
    assert bucket_to_ordinal(0.0) == 1
    assert bucket_to_ordinal(1.0) == 5
    assert bucket_to_ordinal(0.5) == 3
    assert bucket_to_ordinal(1.5) == 5  # clamped
    assert bucket_to_ordinal(-1.0) == 1  # clamped


# ── gold-set loader ───────────────────────────────────────────────


def test_seed_gold_set_loads_and_validates() -> None:
    tasks = load_gold_set()
    assert tasks, "seed gold set is empty"
    axes = {t.axis for t in tasks}
    assert "long-context-recall" in axes
    assert all(t.axis in llm_catalog.CAPABILITY_AXES for t in tasks)


def test_unknown_axis_rejected(tmp_path: Any) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('[{"task_id": "x", "axis": "vibes", "scorer": "needle"}]')
    with pytest.raises(BadInput):
        load_gold_set(bad)


# ── harness (stub dispatch, no model) ─────────────────────────────


def _stub_dispatch(answers: dict[str, Any]) -> Any:
    """A dispatch_fn returning canned (text, data) keyed by the task's needle."""

    def _d(req: Any) -> Any:
        # The stub answers by echoing whatever the prompt asks — the tests set
        # up prompts so the right answer is derivable; simplest is to key on a
        # sentinel embedded per-request via req.prompt lookup.
        text, data = answers.get(req.model, ("", None))
        return SimpleNamespace(text=text, data=data, error=None)

    return _d


def test_run_eval_perfect_model_scores_5(store: Any) -> None:
    from precis.utils.llm.router import Tier

    llm_catalog.upsert_card(store, model_id="stub-perfect", text="A stub model.")
    tasks = [
        GoldTask("n1", "long-context-recall", "needle", "find it", {"needle": "AB-9"}),
    ]
    # The stub returns the needle in its text → score 1.0 → ordinal 5.
    dispatch = _stub_dispatch({"stub-perfect": ("the answer is AB-9", None)})
    report = run_eval(
        store,
        model="stub-perfect",
        tier=Tier.CLOUD_SMALL,
        tasks=tasks,
        dispatch_fn=dispatch,
        record=True,
    )
    assert report.ordinals["long-context-recall"] == 5
    # recorded onto the card as measured-eval
    ref = store.find_ref_by_meta(kind="llm", key="model_id", value="stub-perfect")
    cap = (ref.meta or {}).get("capability") or {}
    assert cap["long-context-recall"]["score"] == 5
    assert cap["long-context-recall"]["provenance"] == "measured-eval"


def test_run_eval_wrong_model_scores_1(store: Any) -> None:
    from precis.utils.llm.router import Tier

    llm_catalog.upsert_card(store, model_id="stub-wrong", text="A stub model.")
    tasks = [GoldTask("n1", "long-context-recall", "needle", "?", {"needle": "AB-9"})]
    dispatch = _stub_dispatch({"stub-wrong": ("no clue", None)})
    report = run_eval(
        store,
        model="stub-wrong",
        tier=Tier.CLOUD_SMALL,
        tasks=tasks,
        dispatch_fn=dispatch,
        record=False,
    )
    assert report.ordinals["long-context-recall"] == 1
    assert report.results[0].recorded is False


def test_dispatch_error_scores_zero(store: Any) -> None:
    from precis.utils.llm.router import Tier

    tasks = [GoldTask("n1", "long-context-recall", "needle", "?", {"needle": "AB-9"})]

    def _err(_req: Any) -> Any:
        return SimpleNamespace(text="", data=None, error="transport down")

    report = run_eval(
        store,
        model="stub-err",
        tier=Tier.CLOUD_SMALL,
        tasks=tasks,
        dispatch_fn=_err,
        record=False,
    )
    assert report.results[0].mean_score == 0.0
    assert report.results[0].per_task[0].error == "transport down"


def test_unwired_scorer_is_skipped_not_scored(store: Any) -> None:
    from precis.utils.llm.router import Tier

    tasks = [
        GoldTask("c1", "code", "run_tests", "fix the bug", {}),  # heavy axis, unwired
        GoldTask("n1", "long-context-recall", "needle", "?", {"needle": "AB-9"}),
    ]
    dispatch = _stub_dispatch({"m": ("AB-9", None)})
    report = run_eval(
        store,
        model="m",
        tier=Tier.CLOUD_SMALL,
        tasks=tasks,
        dispatch_fn=dispatch,
        record=False,
    )
    assert "code" not in report.ordinals  # not measured
    assert any("c1" in s and "code" in s for s in report.skipped)
    assert report.ordinals["long-context-recall"] == 5


def test_compare_runs_both_without_recording(store: Any) -> None:
    from precis.utils.llm.router import Tier

    dispatch = _stub_dispatch({"good": ("AB-9 here", None), "bad": ("nope", None)})
    reports = compare(
        store,
        model_a="good",
        model_b="bad",
        tier=Tier.CLOUD_SMALL,
        dispatch_fn=dispatch,
    )
    # compare runs both models over the (seed) gold set, record=False by default
    # so neither card is written — it returns a report per model to render A/B.
    assert set(reports) == {"good", "bad"}
    assert all(hasattr(r, "ordinals") for r in reports.values())

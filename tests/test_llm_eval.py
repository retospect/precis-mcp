"""LLM golden-eval harness (slice 11).

Pure scorer + gold-loader tests run without a DB; the harness tests inject a
stub ``dispatch_fn`` so no real model runs. The record path uses a real card
(``upsert_card``) against real PG to prove ``run_eval`` writes measured-eval
ordinals through the catalog's existing write surface.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

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
    bad.write_text(
        '[{"task_id": "x", "axis": "vibes", "scorer": "needle"}]', encoding="utf-8"
    )
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
        tier=Tier.MEDIUM,
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
        tier=Tier.MEDIUM,
        tasks=tasks,
        dispatch_fn=dispatch,
        record=False,
    )
    assert report.ordinals["long-context-recall"] == 1
    assert report.results[0].recorded is False


def test_dispatch_error_voids_axis_not_scored(store: Any) -> None:
    from precis.utils.llm.router import Tier

    tasks = [GoldTask("n1", "long-context-recall", "needle", "?", {"needle": "AB-9"})]

    def _err(_req: Any) -> Any:
        return SimpleNamespace(text="", data=None, error="transport down")

    report = run_eval(
        store,
        model="stub-err",
        tier=Tier.MEDIUM,
        tasks=tasks,
        dispatch_fn=_err,
        record=True,
    )
    res = report.results[0]
    # Out of the mean, counted, and never written to the card.
    assert res.void and res.errors == 1 and res.n == 0
    assert res.recorded is False
    assert res.per_task[0].error == "transport down"


def test_errored_tasks_stay_out_of_the_mean() -> None:
    from precis.llm_eval.harness import run_axis
    from precis.utils.llm.router import Tier

    tasks = [
        GoldTask(f"n{i}", "long-context-recall", "needle", "?", {"needle": "AB-9"})
        for i in range(3)
    ]
    replies = iter(
        [
            SimpleNamespace(text="AB-9", data=None, error=None, placement="local"),
            SimpleNamespace(text="", data=None, error="timeout", placement=None),
            SimpleNamespace(text="AB-9", data=None, error=None, placement="local"),
        ]
    )
    # An errored reply carries placement None: void, not PlacementMismatch.
    res = run_axis(
        tasks,
        model="m",
        tier=Tier.SMALL,
        dispatch_fn=lambda _r: next(replies),
        placement="local",
    )
    assert res.mean_score == 1.0 and res.n == 2 and res.errors == 1 and res.void


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
        tier=Tier.MEDIUM,
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
        tier=Tier.MEDIUM,
        dispatch_fn=dispatch,
    )
    # compare runs both models over the (seed) gold set, record=False by default
    # so neither card is written — it returns a report per model to render A/B.
    assert set(reports) == {"good", "bad"}
    assert all(hasattr(r, "ordinals") for r in reports.values())


# ── production-path summary scorer ────────────────────────────────

_CHUNK = (
    "The sintered pellets reached a density of 12,000 kg/m3 after 4.5 h at "
    "1200 C. Cracking was observed in the samples quenched from 1400 C."
)
_GOOD = (
    "BRIEF: Sintered pellets reach 12,000 kg/m3 after 4.5 h at 1200 C.\n"
    "DETAIL: Quenching from 1400 C cracks the samples."
)


def _summ(text: str, **expect: Any) -> float:
    from precis.llm_eval.scorers import score_summary

    exp = {"chunk_text": _CHUNK, "nonprose": False, **expect}
    return score_summary(text, None, exp)


def test_summary_good_scores_one() -> None:
    assert _summ(_GOOD) == 1.0


def test_summary_thousands_comma_normalised() -> None:
    assert _summ(_GOOD.replace("12,000", "12000")) == 1.0


def test_summary_invented_number_scores_zero() -> None:
    assert _summ(_GOOD.replace("4.5 h", "7 h")) == 0.0


def test_summary_empty_and_echo_score_zero() -> None:
    assert _summ("") == 0.0
    assert _summ("BRIEF: At most 15 words, self-contained.\nDETAIL: Something.") == 0.0


def test_summary_tag_matching() -> None:
    tag = "BRIEF: (reference list)\nDETAIL: Citations."
    assert _summ(tag) == 0.0  # prose mis-tagged
    assert _summ(tag, nonprose=True) == 1.0
    assert _summ(_GOOD, nonprose=True) == 0.0  # nonprose answered in prose


def test_summary_keypoint_coverage() -> None:
    assert _summ(_GOOD, keypoints=["12,000", "cracks", "zirconia", "quench"]) == 0.75


# ── GoldTask.messages + run_axis passthrough ──────────────────────


def test_gold_task_messages_load_and_validate(tmp_path: Any) -> None:
    import json

    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    p = tmp_path / "g.json"
    base = {"task_id": "m", "axis": "summarize-extract", "scorer": "summary"}
    p.write_text(json.dumps([{**base, "messages": msgs}]), encoding="utf-8")
    assert load_gold_set(p)[0].messages == msgs
    p.write_text(json.dumps([base]), encoding="utf-8")
    assert load_gold_set(p)[0].messages is None
    for bad in ("x", [{"role": "user"}], [{"role": "user", "content": 3}]):
        p.write_text(json.dumps([{**base, "messages": bad}]), encoding="utf-8")
        with pytest.raises(BadInput):
            load_gold_set(p)


def test_run_axis_passes_messages_through() -> None:
    from precis.llm_eval.harness import run_axis
    from precis.utils.llm.router import Tier

    msgs = [
        {"role": "system", "content": "instructions"},
        {"role": "user", "content": "summarise"},
    ]
    task = GoldTask(
        task_id="s1",
        axis="summarize-extract",
        scorer="summary",
        prompt="",
        messages=msgs,
        expect={"chunk_text": _CHUNK, "nonprose": False},
    )
    seen: list[Any] = []

    def _d(req: Any) -> Any:
        seen.append(req)
        return SimpleNamespace(text=_GOOD, data=None, error=None)

    res = run_axis([task], model="m", tier=Tier.SMALL, dispatch_fn=_d)
    assert seen[0].messages == msgs
    # claude transports read only ``prompt``: it carries the flattened messages
    assert seen[0].prompt == "instructions\n\nsummarise"
    assert res.mean_score == 1.0


# ── gold builder pure helpers ─────────────────────────────────────


def test_build_summarize_gold_row_to_task() -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parent.parent / "scripts/llm_eval/build_summarize_gold.py"
    spec = importlib.util.spec_from_file_location("build_summarize_gold", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    row = {
        "chunk_id": 7,
        "ref_id": 3,
        "ord": 2,
        "chunk_kind": "paragraph",
        "text": _CHUNK,
        "section_path": ["Results"],
        "keywords": None,
        "numerics": [],
        "ref_kind": "paper",
        "title": "Sintering",
        "incumbent": "(reference list)\n\nCitations.",
    }
    t = mod.row_to_task(row, "CARD")
    assert t["task_id"] == "summ-7" and t["scorer"] == "summary"
    assert t["axis"] in llm_catalog.CAPABILITY_AXES and t["prompt"] == ""
    assert t["expect"]["nonprose"] is True and t["expect"]["chunk_text"] == _CHUNK
    assert any(_CHUNK in m["content"] for m in t["messages"])
    assert not mod.incumbent_is_tag("Prose brief.\n\nDetail (x).")


def test_cli_compare_prints_mean_and_n(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--compare shows mean/n beside the ordinal so close models separate."""
    import argparse

    import precis.llm_eval as llm_eval_pkg
    from precis.cli.llm import _cmd_eval
    from precis.llm_eval.harness import AxisResult, EvalReport

    def _rep(model: str, mean: float) -> EvalReport:
        res = AxisResult(
            axis="summarize-extract",
            n=40,
            mean_score=mean,
            ordinal=bucket_to_ordinal(mean),
        )
        return EvalReport(model=model, results=[res], skipped=[])

    monkeypatch.setattr(
        llm_eval_pkg,
        "compare",
        lambda store, **kw: {"a": _rep("a", 0.925), "b": _rep("b", 0.9)},
    )
    args = argparse.Namespace(
        model="a", compare="b", tier="small", gold=None, no_record=True
    )
    _cmd_eval(cast(Any, None), args)
    out = capsys.readouterr().out
    assert "5 (0.925/40)" in out and "5 (0.900/40)" in out


# ── placement guard, kept responses, number-rule reporting ────────


def _task(tid: str = "t1") -> GoldTask:
    return GoldTask(
        task_id=tid,
        axis="summarize-extract",
        scorer="summary",
        prompt="p",
        expect={"chunk_text": "It took eight weeks.", "nonprose": False},
    )


def _placed_dispatch(landed: str, seen: list[Any]) -> Any:
    def _d(req: Any) -> Any:
        seen.append(req)
        return SimpleNamespace(
            text="BRIEF: Took 8 weeks.\nDETAIL: Eight weeks.",
            data=None,
            error=None,
            placement=landed,
        )

    return _d


def test_placement_mismatch_raises_not_zero() -> None:
    from precis.llm_eval.harness import PlacementMismatch, run_axis
    from precis.utils.llm.router import Tier

    with pytest.raises(PlacementMismatch, match=r"t1.*'local'.*'cloud'"):
        run_axis(
            [_task()],
            model="m",
            tier=Tier.MEDIUM,
            dispatch_fn=_placed_dispatch("cloud", []),
            placement="local",
        )


def test_placement_match_carries_request_fields_and_keeps_response() -> None:
    from precis.llm_eval.harness import run_axis
    from precis.utils.llm.router import Tier

    seen: list[Any] = []
    ep = {"provider": "llama-swap", "quant": "q4"}
    res = run_axis(
        [_task()],
        model="m",
        tier=Tier.MEDIUM,
        dispatch_fn=_placed_dispatch("local", seen),
        placement="local",
        endpoint=ep,
    )
    assert seen[0].placement == "local" and seen[0].endpoint == ep
    assert res.per_task[0].response.startswith("BRIEF: Took 8 weeks")
    assert res.per_task[0].score > 0


def test_task_max_tokens_sent_and_capped_reply_flagged() -> None:
    from dataclasses import replace

    from precis.llm_eval.harness import run_axis
    from precis.utils.llm.router import Tier

    seen: list[Any] = []

    def _d(req: Any) -> Any:
        seen.append(req)
        used = 220 if len(seen) == 1 else 90
        return SimpleNamespace(
            text="BRIEF: Took 8 weeks.", data=None, error=None, output_tokens=used
        )

    t = replace(_task(), max_tokens=220)
    res = run_axis(
        [t, replace(t, task_id="t2")], model="m", tier=Tier.SMALL, dispatch_fn=_d
    )
    assert [r.max_tokens for r in seen] == [220, 220]
    assert [ts.capped for ts in res.per_task] == [True, False]


def test_gold_max_tokens_must_be_positive_int(tmp_path: Any) -> None:
    import json

    from precis.errors import BadInput
    from precis.llm_eval import load_gold_set

    item = {
        "task_id": "a",
        "axis": "summarize-extract",
        "scorer": "summary",
        "prompt": "p",
        "max_tokens": 0,
    }
    p = tmp_path / "g.json"
    p.write_text(json.dumps([item]), encoding="utf-8")
    with pytest.raises(BadInput, match="max_tokens"):
        load_gold_set(str(p))
    item["max_tokens"] = 220
    p.write_text(json.dumps([item]), encoding="utf-8")
    assert load_gold_set(str(p))[0].max_tokens == 220


def test_compare_threads_per_arm_placement_and_endpoint(store: Any) -> None:
    from precis.utils.llm.router import Tier

    seen: list[Any] = []

    def _d(req: Any) -> Any:
        seen.append(req)
        return SimpleNamespace(text="x", data=None, error=None, placement=req.placement)

    compare(
        store,
        model_a="a",
        model_b="b",
        tier=Tier.MEDIUM,
        dispatch_fn=_d,
        placement_a="local",
        placement_b="cloud",
        endpoint_a={"quant": "q4"},
        endpoint_b=None,
    )
    by_model = {r.model: r for r in seen}
    assert by_model["a"].placement == "local"
    assert by_model["a"].endpoint == {"quant": "q4"}
    assert by_model["b"].placement == "cloud" and by_model["b"].endpoint is None


def test_summary_number_word_in_chunk_allows_digit() -> None:
    from precis.llm_eval.scorers import score_summary

    exp = {"chunk_text": "It took eight weeks.", "nonprose": False}
    assert score_summary("BRIEF: Took 8 weeks.\nDETAIL: Eight weeks.", None, exp) > 0
    # a derived count (not a word in the chunk) stays a zero
    assert score_summary("BRIEF: Took 56 days.\nDETAIL: Eight weeks.", None, exp) == 0


def test_summary_number_rule_zero_cases() -> None:
    from precis.llm_eval.scorers import score_summary, summary_number_rule_zero

    bad = _GOOD.replace("4.5 h", "7 h")
    exp = {"chunk_text": _CHUNK, "nonprose": False}
    assert summary_number_rule_zero(bad, exp) is True
    assert score_summary(bad, None, {**exp, "number_rule": False}) > 0
    assert summary_number_rule_zero(_GOOD, exp) is False  # scores >0 with rule
    assert summary_number_rule_zero("", exp) is False  # zero either way


def test_cli_compare_prints_number_rule_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    import precis.llm_eval as llm_eval_pkg
    from precis.cli.llm import _cmd_eval
    from precis.llm_eval.harness import AxisResult, EvalReport, TaskScore

    bad = _GOOD.replace("4.5 h", "7 h")
    task = GoldTask(
        task_id="s1",
        axis="summarize-extract",
        scorer="summary",
        prompt="",
        expect={"chunk_text": _CHUNK, "nonprose": False},
    )

    def _rep(model: str, text: str, score: float) -> EvalReport:
        res = AxisResult(
            axis="summarize-extract",
            n=1,
            mean_score=score,
            ordinal=bucket_to_ordinal(score),
            per_task=[TaskScore("s1", score, response=text)],
        )
        return EvalReport(model=model, results=[res], skipped=[])

    captured: dict[str, Any] = {}

    def _fake_compare(store: Any, **kw: Any) -> Any:
        captured.update(kw)
        return {"a": _rep("a", _GOOD, 1.0), "b": _rep("b", bad, 0.0)}

    monkeypatch.setattr(llm_eval_pkg, "compare", _fake_compare)
    monkeypatch.setattr(llm_eval_pkg, "load_gold_set", lambda p=None: [task])
    args = argparse.Namespace(
        model="a",
        compare="b",
        tier="small",
        gold=None,
        no_record=True,
        placement_a="local",
        placement_b="cloud",
        endpoint_a='{"quant": "q4"}',
        endpoint_b=None,
    )
    _cmd_eval(cast(Any, None), args)
    out = capsys.readouterr().out
    assert (
        "a: mean with number rule 1.000, without 1.000, number-rule-only zeros 0/1"
        in out
    )
    assert (
        "b: mean with number rule 0.000, without 1.000, number-rule-only zeros 1/1"
        in out
    )
    assert captured["placement_a"] == "local"
    assert captured["endpoint_a"] == {"quant": "q4"}
    args.endpoint_a = "{nope"
    with pytest.raises(BadInput):
        _cmd_eval(cast(Any, None), args)


def test_cli_compare_prints_void_side(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A side with transport errors prints void + the count, never a mean."""
    import argparse

    import precis.llm_eval as llm_eval_pkg
    from precis.cli.llm import _cmd_eval
    from precis.llm_eval.harness import AxisResult, EvalReport

    def _rep(model: str, errors: int) -> EvalReport:
        res = AxisResult(
            axis="long-context-recall",
            n=40 - errors,
            mean_score=0.9,
            ordinal=bucket_to_ordinal(0.9),
            errors=errors,
        )
        return EvalReport(model=model, results=[res], skipped=[])

    monkeypatch.setattr(
        llm_eval_pkg,
        "compare",
        lambda store, **kw: {"a": _rep("a", 3), "b": _rep("b", 0)},
    )
    args = argparse.Namespace(
        model="a", compare="b", tier="small", gold=None, no_record=True
    )
    _cmd_eval(cast(Any, None), args)
    out = capsys.readouterr().out
    assert "void (3/40 err)" in out and "(0.900/40)" in out
    assert "(0.900/37)" not in out


# ── pinned dispatch (gr464223): eval runs the candidate, not the chain ──


class _FakeProvider:
    def __init__(self, calls: list[Any], transport: Any, *, boom: bool = False) -> None:
        self.calls, self.transport, self.boom = calls, transport, boom

    def run(self, req: Any, *, model: str) -> Any:
        from precis.utils.llm.router import LlmResult

        self.calls.append((self.transport, req, model))
        if self.boom:
            raise RuntimeError("wire down")
        return LlmResult(
            text="ok", cost_usd=None, turns_used=None, model=model, tier=req.tier
        )


def _pin_env(
    monkeypatch: pytest.MonkeyPatch, *, boom: bool = False
) -> tuple[list[Any], list[Any]]:
    """Stub provider_for + record_dispatch; return (provider calls, records)."""
    from precis.llm_eval import pinned

    calls: list[Any] = []
    recs: list[Any] = []
    monkeypatch.setattr(
        pinned, "provider_for", lambda t, **kw: _FakeProvider(calls, t, boom=boom)
    )
    monkeypatch.setattr(
        pinned, "record_dispatch", lambda req, res, **kw: recs.append((req, res, kw))
    )
    return calls, recs


def _req(**kw: Any) -> Any:
    from precis.utils.llm.router import LlmRequest, Tier

    return LlmRequest(tier=Tier.MEDIUM, prompt="p", source="llm_eval", **kw)


def _stub_slots(monkeypatch: pytest.MonkeyPatch, slot: Any) -> list[Any]:
    from precis.utils.llm import local_serving

    released: list[Any] = []
    monkeypatch.setattr(local_serving, "acquire", lambda model: slot)
    monkeypatch.setattr(local_serving, "release", lambda s: released.append(s))
    return released


def test_pinned_local_acquires_runs_candidate_and_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.llm_eval.pinned import pinned_dispatch
    from precis.utils.llm.local_serving import LocalSlot
    from precis.utils.llm.router import Transport

    slot = LocalSlot(
        host="h",
        resource="r",
        reserved=True,
        paused=False,
        endpoint="http://x/v1",
        served_model="srv-name",
    )
    released = _stub_slots(monkeypatch, slot)
    calls, recs = _pin_env(monkeypatch)
    res = pinned_dispatch("cand", "local")(_req(model="cand"))
    assert res.error is None and res.placement == "local"
    transport, creq, model = calls[0]
    assert transport is Transport.LOCAL
    assert creq.local_url == "http://x/v1" and model == "srv-name"
    assert released == [slot]
    assert recs[0][0].source == "llm_eval"
    assert recs[0][2]["transport"] is Transport.LOCAL


def test_pinned_local_releases_slot_when_provider_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.llm_eval.pinned import pinned_dispatch
    from precis.utils.llm.local_serving import LocalSlot

    slot = LocalSlot(host="h", resource="r", reserved=True, paused=False)
    released = _stub_slots(monkeypatch, slot)
    _pin_env(monkeypatch, boom=True)
    res = pinned_dispatch("cand", "local")(_req())
    assert res.error and "wire down" in res.error
    assert released == [slot]


def test_pinned_local_not_served_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from precis.llm_eval.pinned import pinned_dispatch

    released = _stub_slots(monkeypatch, None)
    calls, _ = _pin_env(monkeypatch)
    res = pinned_dispatch("cand", "local")(_req())
    assert res.error and "not served on any host" in res.error
    assert not calls and released == []


def test_pinned_local_paused_is_error_and_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.llm_eval.pinned import pinned_dispatch
    from precis.utils.llm.local_serving import LocalSlot

    slot = LocalSlot(host="h", resource="r", reserved=False, paused=True)
    released = _stub_slots(monkeypatch, slot)
    calls, _ = _pin_env(monkeypatch)
    res = pinned_dispatch("cand", "local")(_req())
    assert res.error and "all local slots busy" in res.error
    assert not calls and released == [slot]


def test_pinned_cloud_picks_transport_by_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.budget import breaker
    from precis.llm_eval.pinned import pinned_dispatch
    from precis.utils.llm.router import Transport

    seen: list[Any] = []

    def _gate(tier: Any, **kw: Any) -> None:
        seen.append(kw)

    monkeypatch.setattr(breaker, "gate_tier", _gate)
    calls, _ = _pin_env(monkeypatch)
    r1 = pinned_dispatch("claude-haiku-4-5", "cloud")(_req())
    r2 = pinned_dispatch("qwen3-235b", "cloud")(_req())
    assert [c[0] for c in calls] == [Transport.CLAUDE_P, Transport.OPENAI_COMPAT]
    assert [c[2] for c in calls] == ["claude-haiku-4-5", "qwen3-235b"]
    assert r1.placement == r2.placement == "cloud"
    assert seen[0]["local"] is False and seen[0]["bare"] is False


def test_pinned_cloud_honours_tripped_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.budget import breaker
    from precis.llm_eval.pinned import pinned_dispatch

    monkeypatch.setattr(breaker, "gate_tier", lambda tier, **kw: "daily cap hit")
    calls, recs = _pin_env(monkeypatch)
    res = pinned_dispatch("claude-haiku-4-5", "cloud")(_req())
    assert res.error == "daily cap hit" and not calls and not recs


def test_run_eval_with_placement_uses_pinned_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.budget import breaker
    from precis.utils.llm.router import Tier

    monkeypatch.setattr(breaker, "gate_tier", lambda tier, **kw: None)
    calls, _ = _pin_env(monkeypatch)
    run_eval(
        cast(Any, None),
        model="claude-cand",
        tier=Tier.MEDIUM,
        tasks=[_task()],
        placement="cloud",
        record=False,
    )
    assert calls and all(c[2] == "claude-cand" for c in calls)


def test_cli_void_axis_prints_first_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    import precis.llm_eval as llm_eval_pkg
    from precis.cli.llm import _cmd_eval
    from precis.llm_eval.harness import AxisResult, EvalReport, TaskScore

    def _rep(model: str, err: str) -> EvalReport:
        res = AxisResult(
            axis="summarize-extract",
            n=0,
            mean_score=0.0,
            ordinal=1,
            per_task=[TaskScore("t1", 0.0, error=err), TaskScore("t2", 0.0, error="x")],
            errors=2,
        )
        return EvalReport(model=model, results=[res], skipped=[])

    long_err = "boom " * 100
    monkeypatch.setattr(
        llm_eval_pkg,
        "compare",
        lambda store, **kw: {"a": _rep("a", "not served"), "b": _rep("b", long_err)},
    )
    args = argparse.Namespace(
        model="a", compare="b", tier="small", gold=None, no_record=True
    )
    _cmd_eval(cast(Any, None), args)
    out = capsys.readouterr().out
    assert "a summarize-extract: first error: not served" in out
    assert "operator chain" in out  # no placements -> warning
    line = next(ln for ln in out.splitlines() if ln.startswith("b summarize"))
    assert len(line) < 260

    monkeypatch.setattr(
        llm_eval_pkg, "run_eval", lambda store, **kw: _rep("a", "single err")
    )
    single = argparse.Namespace(
        model="a", compare=None, tier="small", gold=None, no_record=True
    )
    _cmd_eval(cast(Any, None), single)
    assert "first error: single err" in capsys.readouterr().out


def test_run_binds_the_four_process_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    from precis import budget as budget_pkg
    from precis import route_log, secrets, settings
    from precis.cli import llm as cli_llm

    fake = object()
    bound: list[str] = []

    def _rec(name: str) -> Any:
        return lambda s: bound.append(name) if s is fake else None

    monkeypatch.setattr(cli_llm.Store, "connect", lambda dsn: fake)
    monkeypatch.setattr(cli_llm, "resolve_dsn", lambda url: "dsn")
    monkeypatch.setattr(secrets, "adopt_process_store", _rec("secrets"))
    monkeypatch.setattr(route_log, "bind_store", _rec("route_log"))
    monkeypatch.setattr(budget_pkg, "bind_store", _rec("budget"))
    monkeypatch.setattr(settings, "bind_store", _rec("settings"))
    seen: list[Any] = []
    monkeypatch.setattr(cli_llm, "_cmd_list", lambda store: seen.append(store))
    cli_llm.run(argparse.Namespace(llm_cmd="list", database_url=None))
    assert sorted(bound) == ["budget", "route_log", "secrets", "settings"]
    assert seen == [fake]

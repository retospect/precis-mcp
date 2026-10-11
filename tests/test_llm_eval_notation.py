"""Notation A/B eval (gr336349): task generation, arm accept sets, slip scoring."""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any

import pytest

from precis.errors import BadInput
from precis.llm_eval.notation import (
    ARMS,
    build_prompt,
    build_tasks,
    check_arm,
    classify,
    extract_answer,
    format_report,
    reply_answer,
    run_notation_eval,
)
from precis.utils.units import format_quantity

TASKS = build_tasks()
NM = TASKS[0]  # 3.99 nanometres
MN = next(t for t in TASKS if t.dimension == "force" and t.intent.startswith("35 "))
VOL = next(t for t in TASKS if t.intent == "350 millilitres")
_BASE = {"length": "m", "force": "N", "mass": "kg", "volume": "m^3"}


def test_tasks_are_deterministic_and_span_scales() -> None:
    assert build_tasks() == TASKS
    assert len({t.task_id for t in TASKS}) == len(TASKS) == 16
    lengths = [t.expect_si for t in TASKS if t.dimension == "length"]
    assert min(lengths) == pytest.approx(3.99e-9)
    assert max(lengths) == pytest.approx(12000.0)
    assert {t.dimension for t in TASKS} == {"length", "force", "mass", "volume"}
    assert NM.intent == "3.99 nanometres"
    mass = next(t for t in TASKS if t.intent == "7 micrograms")
    assert mass.expect_si == pytest.approx(7e-9)  # kg
    assert VOL.expect_si == pytest.approx(3.5e-4)  # m^3


def test_prompts_name_intent_and_arm_rule() -> None:
    p = build_prompt(NM, "si_prefix")
    assert "3.99 nanometres" in p and '{"answer"' in p
    assert "e-notation" in build_prompt(NM, "e_notation")
    with pytest.raises(BadInput):
        build_prompt(NM, "nope")


def test_extract_answer() -> None:
    assert extract_answer("thinking\nANSWER: `3.99 nm`.") == "3.99 nm"
    assert extract_answer("3.99 nm") == "3.99 nm"


def test_arm_accept_sets() -> None:
    check_arm("3.99 nm", "length", "si_prefix")
    check_arm("0.5 m", "length", "si_prefix")
    with pytest.raises(BadInput):
        check_arm("3.99e-9 m", "length", "si_prefix")
    check_arm("3.99e-9 m", "length", "e_notation")
    check_arm("0.0005 m", "length", "e_notation")  # plain base unit is legal
    check_arm("3.5e-4 m^3", "volume", "e_notation")
    for bad in ("3.99 nm", "3.99e-6 mm"):
        with pytest.raises(BadInput):
            check_arm(bad, "length", "e_notation")


def test_classify_exact_slip_wrong_parse_fail() -> None:
    assert classify(NM, "ANSWER: 3.99 nm", "si_prefix").kind == "exact"
    assert classify(NM, "ANSWER: 3.99e-9 m", "e_notation").kind == "exact"
    s = classify(NM, "ANSWER: 3.99 µm", "si_prefix")  # 1000x high
    assert (s.kind, s.slip_exp) == ("slip", 3)
    s = classify(NM, "ANSWER: 3.99e-8 m", "e_notation")  # 10x high
    assert (s.kind, s.slip_exp) == ("slip", 1)
    s = classify(NM, "ANSWER: 3.99e-12 m", "e_notation")  # 1000x low
    assert (s.kind, s.slip_exp) == ("slip", -3)
    assert classify(NM, "ANSWER: 4.2 nm", "si_prefix").kind == "wrong"
    # wrong-arm notation and unparseable text are parse failures, not slips
    assert classify(NM, "ANSWER: 3.99e-9 m", "si_prefix").kind == "parse_fail"
    assert classify(NM, "ANSWER: 3.99 nm", "e_notation").kind == "parse_fail"
    assert classify(NM, "ANSWER: three nm", "si_prefix").kind == "parse_fail"
    assert classify(NM, "ANSWER: 3.99", "si_prefix").kind == "parse_fail"  # bare


def test_classify_other_dimensions() -> None:
    assert classify(MN, "ANSWER: 35 mN", "si_prefix").kind == "exact"
    assert classify(MN, "ANSWER: 3.5e-2 N", "e_notation").kind == "exact"
    s = classify(MN, "ANSWER: 35 N", "si_prefix")
    assert (s.kind, s.slip_exp) == ("slip", 3)
    assert classify(VOL, "ANSWER: 350 ml", "si_prefix").kind == "exact"
    assert classify(VOL, "ANSWER: 3.5e-4 m^3", "e_notation").kind == "exact"


def test_run_with_fake_model_counts_per_arm() -> None:
    """Fake model: perfect under si_prefix; under e_notation it slips 1000x on
    nano-scale lengths and emits a prefixed unit once."""
    seen: list[Any] = []

    def reply(answer: str) -> Any:
        # Real claude_p transport: raw text + parsed dict in ``data``.
        return SimpleNamespace(
            text=f'{{"answer": "{answer}"}}', data={"answer": answer}, error=None
        )

    def fake(req: Any) -> Any:
        seen.append(req)
        task = next(t for t in TASKS if t.intent in req.prompt)
        if "e-notation" not in req.prompt:
            return reply(format_quantity(task.expect_si, task.dimension))
        if task.task_id == TASKS[1].task_id:
            return reply("20 um")
        v = task.expect_si
        if task.dimension == "length" and v < 1e-8:
            v *= 1000
        return reply(f"{v!r} {_BASE[task.dimension]}")

    res = run_notation_eval(model="fake", tier="medium", dispatch_fn=fake)
    assert set(res) == set(ARMS)
    assert len(seen) == 2 * len(TASKS)
    a, b = res["si_prefix"], res["e_notation"]
    assert (a.n, a.exact, a.slip_total, a.parse_fail) == (16, 16, 0, 0)
    assert (b.n, b.exact, b.slips, b.parse_fail, b.wrong) == (16, 14, {3: 1}, 1, 0)
    assert math.isclose(a.exact_rate, 1.0)
    assert "si_prefix" in format_report(res)


def test_transport_errors_are_kept_out_of_n() -> None:
    res = run_notation_eval(
        model="fake",
        tier="medium",
        dispatch_fn=lambda req: SimpleNamespace(text="", error="boom"),
        arms=("si_prefix",),
    )
    assert (res["si_prefix"].n, res["si_prefix"].errors) == (0, 16)


def test_reply_answer_parsed_dict_and_json_text() -> None:
    """Regression: claude -p hands back JSON as ``data`` (dict), never an
    ``ANSWER:`` line; the old parser scored every such reply parse_fail."""
    parsed = SimpleNamespace(text="raw", data={"answer": "3.99 nm"}, error=None)
    assert reply_answer(parsed) == "3.99 nm"
    embedded = SimpleNamespace(text='ok {"answer": "2 mN"} done', data=None)
    assert reply_answer(embedded) == "2 mN"
    assert reply_answer(SimpleNamespace(text="3.99 nm", data=None)) == "3.99 nm"
    res = run_notation_eval(
        model="f", tier="medium", dispatch_fn=lambda r: parsed, arms=("si_prefix",)
    )["si_prefix"]
    # Only the 3.99 nm task is exact; the 3.99 nm answer is the wrong
    # dimension elsewhere. The point: the dict is read (exact == 1, not 0).
    assert (res.n, res.errors, res.exact) == (16, 0, 1)

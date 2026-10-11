"""Quantity-notation A/B eval (gr336349) — which notation do models author most reliably?

Question (Reto 2026-09-12, "test it, don't argue it"): when an agent must
*write* a dimensioned value, does it get the scale right more often with

* arm ``si_prefix`` — SI-prefixed units, no exponent: ``3.99 nm``, ``2.2 mN``; or
* arm ``e_notation`` — plain base units with e-notation: ``3.99e-9 m``, ``2.2e-3 N``?

Hypotheses: the prefix form removes two slip classes (zero-run counting and
the exponent digit) and matches display output; e-notation is uniform across
scales but keeps the one-character exponent slip.

Method: a fixed task set spanning nano -> mega (:func:`build_tasks`, no
randomness) states each intent in *words* ("3.99 nanometres") so neither arm's
spelling is leaked, and asks for a single quantity in the arm's notation.
Each reply is parsed with the repo's own :func:`precis.utils.units.parse_quantity`
after the arm's accept set is enforced (:func:`check_arm`), then classified
(:func:`classify`) as ``exact`` / ``slip`` (off by a power of ten; tallied per
exponent, so 10x and 1000x are separate) / ``wrong`` / ``parse_fail``.

Run (spends model tokens; ~32 short calls per model per repeat)::

    uv run python -m precis.llm_eval.notation --model <model-id> --tier medium
    uv run python -m precis.llm_eval.notation --dry-run     # print prompts only

Uses the live router (:func:`precis.utils.llm.router.route`) like
:func:`precis.llm_eval.harness.run_eval`; tests inject a fake ``dispatch_fn``.
Not wired into ``record_eval`` — this is a one-off decision experiment, not a
capability axis. Deferred: retry cost after the reject-with-hint.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from precis.errors import BadInput
from precis.utils.units import Dimension, parse_quantity

ARMS: tuple[str, ...] = ("si_prefix", "e_notation")

#: Relative error under which a value counts as exactly right.
EXACT_RTOL = 1e-6

_EXP_RE = re.compile(r"\d\s*[eE]\s*[+-]?\s*\d")
_QTY_RE = re.compile(r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*(.+?)\s*$")

#: Unit spellings the e-notation arm accepts (plain, un-prefixed base units).
_BASE_UNITS: dict[str, frozenset[str]] = {
    "length": frozenset({"m"}),
    "force": frozenset({"N"}),
    "mass": frozenset({"kg"}),
    "volume": frozenset({"m3", "m^3", "m**3", "m³"}),
}

#: Spoken name of the unit each spec's prefix attaches to.
_UNIT_WORDS: dict[str, str] = {
    "length": "metres",
    "force": "newtons",
    "mass": "grams",
    "volume": "litres",
}
_PREFIX_WORDS = {
    -9: "nano",
    -6: "micro",
    -3: "milli",
    0: "",
    3: "kilo",
    6: "mega",
}
_QUANTITY_NAME = {
    "length": "the length",
    "force": "the force",
    "mass": "the mass",
    "volume": "the volume",
}

#: (dimension, mantissa, prefix exponent relative to the *named* unit —
#: metre / newton / gram / litre). Spans nano -> mega where physical.
_SPECS: tuple[tuple[Dimension, float, int], ...] = (
    ("length", 3.99, -9),
    ("length", 20.0, -6),
    ("length", 0.5, -3),
    ("length", 4.7, 0),
    ("length", 12.0, 3),
    ("force", 8.0, -9),
    ("force", 2.5, -6),
    ("force", 35.0, -3),
    ("force", 6.0, 0),
    ("force", 1.2, 3),
    ("force", 3.3, 6),
    ("mass", 7.0, -6),
    ("mass", 250.0, -3),
    ("mass", 1.8, 3),
    ("volume", 15.0, -6),
    ("volume", 350.0, -3),
)


@dataclass(frozen=True, slots=True)
class NotationTask:
    task_id: str
    dimension: Dimension
    #: Intended value in SI base (metre / newton / kilogram / cubic metre).
    expect_si: float
    #: The intent as spoken to the model, e.g. ``3.99 nanometres``.
    intent: str


def _si_base(dimension: str, mantissa: float, exp: int) -> float:
    """Intent in SI base (mass: named unit is the gram, base is the kilogram)."""
    v = mantissa * 10.0**exp
    return v / 1000.0 if dimension in ("mass", "volume") else v


def build_tasks() -> list[NotationTask]:
    """The fixed, ordered task set (deterministic — no randomness)."""
    tasks: list[NotationTask] = []
    for dim, mant, exp in _SPECS:
        unit_word = _UNIT_WORDS[dim]
        word = _PREFIX_WORDS[exp] + unit_word
        tasks.append(
            NotationTask(
                task_id=f"notation-{dim}-{len(tasks):02d}",
                dimension=dim,
                expect_si=_si_base(dim, mant, exp),
                intent=f"{mant:g} {word}",
            )
        )
    return tasks


_ARM_INSTRUCTIONS = {
    "si_prefix": (
        "Write the value with an SI-prefixed unit and NO exponent, "
        "e.g. `3.99 nm`, `2.2 mN`, `350 ml`."
    ),
    "e_notation": (
        "Write the value in plain base units (m, N, kg, m^3 — no SI prefix) "
        "using e-notation for the number, e.g. `3.99e-9 m`, `2.2e-3 N`, `3.5e-4 m^3`."
    ),
}


def build_prompt(task: NotationTask, arm: str) -> str:
    """The prompt for one (task, arm): intent in words + the arm's notation rule."""
    if arm not in _ARM_INSTRUCTIONS:
        raise BadInput(f"unknown notation arm {arm!r}", options=list(ARMS))
    return (
        "You are authoring a design parameter for a CAD/simulation tool. "
        f"The design intent is {_QUANTITY_NAME[task.dimension]} "
        f"equal to {task.intent}.\n"
        f"{_ARM_INSTRUCTIONS[arm]}\n"
        'Reply with a JSON object and nothing else: {"answer": "<quantity>"}.'
    )


def reply_answer(res: Any) -> str:
    """The answer from a router result.

    The claude_p transport returns the parsed JSON as ``res.data``; otherwise
    take the last ``{"answer": ...}`` object in ``res.text``, else the text.
    """
    data = getattr(res, "data", None)
    if isinstance(data, dict) and data.get("answer") is not None:
        return str(data["answer"])
    text = getattr(res, "text", "") or ""
    decoder = json.JSONDecoder()
    found: str | None = None
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text, i)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("answer") is not None:
            found = str(obj["answer"])
    return found if found is not None else text


def extract_answer(text: str) -> str:
    """The quantity string from a reply: the last ``ANSWER:`` line, else the text."""
    matches = re.findall(r"answer\s*[:=]\s*(.+)", text or "", re.IGNORECASE)
    raw = matches[-1] if matches else (text or "").strip()
    return raw.strip().rstrip(".").strip("`*").strip().rstrip(".").strip()


def check_arm(answer: str, dimension: str, arm: str) -> None:
    """Enforce the arm's accept set; raise :class:`BadInput` on a violation."""
    if arm == "si_prefix":
        if _EXP_RE.search(answer):
            raise BadInput(
                f"{answer!r}: e-notation is not accepted in the si_prefix arm"
            )
        return
    if arm == "e_notation":
        m = _QTY_RE.match(answer)
        if not m or m.group(2).replace(" ", "") not in _BASE_UNITS[dimension]:
            raise BadInput(
                f"{answer!r}: e_notation arm accepts only plain base units "
                f"{sorted(_BASE_UNITS[dimension])}"
            )
        return
    raise BadInput(f"unknown notation arm {arm!r}", options=list(ARMS))


@dataclass(frozen=True, slots=True)
class Outcome:
    task_id: str
    kind: str  # exact | slip | wrong | parse_fail
    value: float | None = None
    #: For ``slip``: the power-of-ten exponent the value is off by (+3 = 1000x high).
    slip_exp: int | None = None
    error: str | None = None


def classify(task: NotationTask, response: str, arm: str) -> Outcome:
    """Parse ``response`` under ``arm`` and compare to the intent."""
    answer = extract_answer(response)
    try:
        check_arm(answer, task.dimension, arm)
        # parse_quantity wants pint syntax: `^` is xor in Python eval.
        value = parse_quantity(answer.replace("^", "**"), task.dimension)
    except BadInput as exc:
        return Outcome(task.task_id, "parse_fail", error=str(exc))
    if not math.isfinite(value) or value <= 0:
        return Outcome(task.task_id, "wrong", value=value)
    if abs(value - task.expect_si) <= EXACT_RTOL * abs(task.expect_si):
        return Outcome(task.task_id, "exact", value=value)
    log_ratio = math.log10(value / task.expect_si)
    k = round(log_ratio)
    if k != 0 and abs(log_ratio - k) < 1e-6:
        return Outcome(task.task_id, "slip", value=value, slip_exp=k)
    return Outcome(task.task_id, "wrong", value=value)


@dataclass(frozen=True, slots=True)
class ArmResult:
    arm: str
    n: int
    exact: int
    wrong: int
    parse_fail: int
    #: exponent -> count (e.g. {3: 1, -1: 2}); 10x and 1000x are separate keys.
    slips: dict[int, int] = field(default_factory=dict)
    errors: int = 0  # transport errors, kept out of ``n``
    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def slip_total(self) -> int:
        return sum(self.slips.values())

    @property
    def exact_rate(self) -> float:
        return self.exact / self.n if self.n else 0.0


def summarize(arm: str, outcomes: list[Outcome], errors: int = 0) -> ArmResult:
    c = Counter(o.kind for o in outcomes)
    slips = Counter(o.slip_exp for o in outcomes if o.kind == "slip")
    return ArmResult(
        arm=arm,
        n=len(outcomes),
        exact=c["exact"],
        wrong=c["wrong"],
        parse_fail=c["parse_fail"],
        slips={int(k): v for k, v in sorted(slips.items()) if k is not None},
        errors=errors,
        outcomes=outcomes,
    )


def run_notation_eval(
    *,
    model: str,
    tier: Any,
    dispatch_fn: Callable[[Any], Any] | None = None,
    arms: tuple[str, ...] = ARMS,
    repeats: int = 1,
) -> dict[str, ArmResult]:
    """Run every task under every arm (``repeats`` times) and score per arm."""
    from precis.utils.llm.router import LlmRequest

    disp: Callable[[Any], Any]
    if dispatch_fn is None:
        from precis.utils.llm.router import route as disp
    else:
        disp = dispatch_fn
    tasks = build_tasks()
    out: dict[str, ArmResult] = {}
    for arm in arms:
        outcomes: list[Outcome] = []
        errors = 0
        for _ in range(repeats):
            for t in tasks:
                res = disp(
                    LlmRequest(
                        tier=tier,
                        prompt=build_prompt(t, arm),
                        model=model,
                        max_tokens=64,
                        source="llm_eval",
                    )
                )
                if getattr(res, "error", None):
                    errors += 1
                    continue
                outcomes.append(classify(t, reply_answer(res), arm))
        out[arm] = summarize(arm, outcomes, errors)
    return out


def format_report(results: dict[str, ArmResult]) -> str:
    lines = ["arm          n  exact  slips(exp:count)  wrong  parse_fail  err"]
    for r in results.values():
        slips = ",".join(f"{k:+d}:{v}" for k, v in r.slips.items()) or "-"
        lines.append(
            f"{r.arm:<11} {r.n:>2}  {r.exact:>5}  {slips:<16}  {r.wrong:>5}  "
            f"{r.parse_fail:>10}  {r.errors:>3}   exact_rate={r.exact_rate:.2f}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model", default="")
    ap.add_argument("--tier", default="medium")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true", help="print prompts, no calls")
    args = ap.parse_args(argv)
    if args.dry_run:
        for arm in ARMS:
            for t in build_tasks():
                print(
                    f"[{arm}] {t.task_id} expect={t.expect_si!r}\n{build_prompt(t, arm)}\n"
                )
        return 0
    from precis.utils.llm.router import Tier

    results = run_notation_eval(
        model=args.model, tier=Tier(args.tier), repeats=args.repeats
    )
    print(format_report(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

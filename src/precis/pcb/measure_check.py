"""The ``measure`` check rule: a violated placement measure is a finding.

Ruling (Reto 2026-11-01): "decap and crystal, within some limit, make it
fail drc". The limit is the measure's own ``goal`` and its ``strength`` sets
the tier: a VIOLATED ``hard`` measure is an ``error``, a ``soft`` one a
``warn``. Every other row adds nothing. ``gauge`` is a ruler, ``invariant``
belongs to the always-valid gate
(:func:`precis.pcb.session.invariant_measure_findings`), and a ``pending``
row could not be evaluated, which is not a violation.

The rule covers the pair metrics :func:`precis.pcb.eyes.evaluate_measures`
judges: ``proximity``, ``separation`` and ``align``. ``height`` is not a
pair and stays a ``view='measures'`` verdict. The verdict, the binding pair
and the residual all come from that evaluator, so ``view='measures'`` and
the check surface cannot disagree about a measure.

:func:`precis.pcb.check_run.run` adds the findings to ``view='check'`` and
``view='drc'`` at every phase except a claimed ``netlist``. The pre-route
placement gate (:func:`precis.pcb.drc.run_placement_drc`) leaves them out:
there the anneal prices a ``hard`` measure as a cost, never as a legality
wall (:mod:`precis.pcb.optimize`), so a place job does not refuse on one.

No store access: unlike ``check_run`` this module ships in the precis-geom
wheel, beside the ``eyes`` and ``drc`` it reads.
"""

from __future__ import annotations

from typing import Any

from precis.pcb import eyes
from precis.pcb.drc import DrcFinding

__all__ = ["MEASURE_METRICS", "MEASURE_RULE", "MEASURE_SEVERITY", "measure_findings"]

#: The finding's rule name on ``view='check'`` and ``view='drc'``.
MEASURE_RULE = "measure"

#: Metrics the rule judges: the placement-pair verdicts of
#: :func:`precis.pcb.eyes.evaluate_measures`.
MEASURE_METRICS = frozenset({"proximity", "separation", "align"})

#: ``strength`` -> finding severity. Any other strength adds no finding.
MEASURE_SEVERITY = {"hard": "error", "soft": "warn"}


def _strength(m: dict[str, Any]) -> str:
    return str(m.get("strength") or "gauge").strip().lower()


def _limit(bound: str, goal: float) -> str:
    """The limit the evaluator held the measured value to (``bound`` is
    :func:`precis.pcb.eyes.measure_bound`'s)."""
    if bound == "upper":
        return f"≤ {goal:g} mm"
    if bound == "lower":
        return f"≥ {goal:g} mm"
    return f"{goal:g} ± {eyes.target_tol_mm(goal):g} mm"


def measure_findings(
    graph: dict[str, Any],
    measures: list[dict[str, Any]],
    features: list[dict[str, Any]] | None = None,
) -> list[DrcFinding]:
    """One ``measure`` finding per VIOLATED hard or soft pair measure.

    ``measures`` are ``Store.pcb_measures_list`` rows judged against
    ``graph``'s placement; ``features`` resolve an align ``feature_id``
    operand. ``where`` names the binding pair, ``objects`` carries its
    refdes and ``margin_mm`` is the residual, negated."""
    judged = [
        m
        for m in measures
        if _strength(m) in MEASURE_SEVERITY
        and str(m.get("metric") or "").strip().lower() in MEASURE_METRICS
    ]
    if not judged:
        return []
    out: list[DrcFinding] = []
    results = eyes.evaluate_measures(graph, judged, features)
    for m, res in zip(judged, results, strict=True):
        if res["verdict"] != "VIOLATED":
            continue
        metric = str(res["metric"])
        strength = _strength(m)
        excess = float(res["excess"])
        if metric == "align":
            axis = str((m.get("meta") or {}).get("axis") or "xy").strip().lower()
            measured = f"{axis} offset error {res['value']:g} mm"
            bound = "upper"
        else:
            measured = f"centre gap {res['value']:g} mm"
            bound = eyes.measure_bound(m.get("direction"), metric)
        mid = m.get("measure_id")
        head = f"{strength} {metric}" + ("" if mid is None else f" (measure #{mid})")
        reason = str(m.get("reason") or "").strip()
        out.append(
            DrcFinding(
                rule=MEASURE_RULE,
                severity=MEASURE_SEVERITY[strength],
                where="measure " + "–".join(str(end) for end in res["pair"]),
                detail=(
                    f"{head}: {measured}, limit {_limit(bound, float(res['goal']))}, "
                    f"residual {excess:g} mm" + (f" — {reason}" if reason else "")
                ),
                objects=tuple({"refdes": str(r)} for r in res["parts"]),
                margin_mm=-excess,
            )
        )
    return out

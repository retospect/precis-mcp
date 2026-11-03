"""Negative-TOF classification labels and the Pareto inclusion rule.

The catpath engine (>= the tof_sign cut, gr559294) classifies a negative
``tof_mf`` instead of failing the kinetics job: it stamps
``kinetics_v2.mf_status.tof_sign`` — ABSENT when ``tof_mf >= 0`` or on any
older record — with one of five classes::

    {"class": "noise"}                                     # under the noise floor
    {"class": "reverse" | "consumed_by_other" | "unresolved"
           | "broken",
     "sinks":   {gas: rate < 0},   # most consumed first
     "sources": {gas: rate > 0},   # most released first
     "reason"?: str}               # "broken": the element imbalance

``"unresolved"`` means ``q_over_k`` was unavailable; ``"broken"`` also sets
``mf_status.ok`` false. Non-noise negatives carry a kinetics warning naming
the gases on the same record.

Two pure reads, one module, so the MCP text, the web panel and the quest
harvest describe the same record identically: :func:`tof_sign_label` turns
the class (+ its gas lists) into plain words, and :func:`pareto_keeps`
decides whether the solve still ranks — noise counts as a TOF of about
zero (kept), every other class is left off the Pareto front and listed
below it with the label as the reason.
"""

from __future__ import annotations

import math
from typing import Any

#: Sinks/sources rendered beside a non-noise label "where there is room" —
#: the engine sorts both dicts by magnitude already (most consumed / most
#: released first), so the cap is a slice, not a re-sort.
_GAS_CAP = 3


def tof_sign(v2: Any) -> dict[str, Any] | None:
    """The ``mf_status.tof_sign`` record of a ``kinetics_v2`` dict — ``None``
    when absent (``tof_mf >= 0``, a chain/KMC-rung record, or any pre-tof_sign
    engine), so every caller degrades to its unclassified rendering for free."""
    if not isinstance(v2, dict):
        return None
    st = v2.get("mf_status")
    if not isinstance(st, dict):
        return None
    sign = st.get("tof_sign")
    return sign if isinstance(sign, dict) and sign.get("class") else None


def _top_gases(table: Any, n: int = _GAS_CAP) -> list[str]:
    """The first ``n`` gas names of a sinks/sources dict, engine order
    (most consumed / most released first). Only finite rates count — a
    ``null`` rate is an absent measurement, never a named gas."""
    if not isinstance(table, dict):
        return []
    return [
        str(g)
        for g, r in table.items()
        if isinstance(r, (int, float)) and not isinstance(r, bool) and math.isfinite(r)
    ][:n]


def tof_sign_label(sign: dict[str, Any]) -> str:
    """The plain-words label for a classified negative ``tof_mf`` — the same
    sentence every surface (pathway panel, pathway card, ``view='kinetics'``,
    the Pareto exclusion reason) shows for the class:

    * ``noise`` → "about zero (below noise floor)"
    * ``reverse`` → "runs in reverse (Q/K > 1)"
    * ``consumed_by_other`` → "consumed by another reaction: <top sinks>"
    * ``unresolved`` → the same, marked "cause unknown" (``q_over_k`` was
      unavailable, so reverse-vs-consumption is undecidable)
    * ``broken`` → "broken: <reason>" (the engine's element imbalance)
    * anything else → "unclassified" — a class this reader predates is
      named, never guessed at.
    """
    cls = str(sign.get("class") or "")
    if cls == "noise":
        return "about zero (below noise floor)"
    if cls == "reverse":
        return "runs in reverse (Q/K > 1)"
    if cls in ("consumed_by_other", "unresolved"):
        sinks = ", ".join(_top_gases(sign.get("sinks")))
        label = "consumed by another reaction" + (
            " (cause unknown)" if cls == "unresolved" else ""
        )
        return f"{label}: {sinks}" if sinks else label
    if cls == "broken":
        reason = sign.get("reason")
        return f"broken: {reason}" if reason else "broken"
    return f"unclassified ({cls or '?'})"


def tof_sign_detail(sign: dict[str, Any]) -> str:
    """The "sinks … · sources …" detail line for a non-noise class — the
    top :data:`_GAS_CAP` gases of each net-flux table with their signed
    rates. ``""`` on a class that carries neither table (noise, a drifted
    record) so callers can skip the line entirely."""
    sinks = _top_gases(sign.get("sinks"))
    sources = _top_gases(sign.get("sources"))
    parts = []
    if sinks:
        parts.append("sinks: " + ", ".join(sinks))
    if sources:
        parts.append("sources: " + ", ".join(sources))
    return " · ".join(parts)


def pareto_keeps(sign: dict[str, Any] | None) -> bool:
    """The Pareto inclusion rule for a classified negative ``tof_mf``:
    ``noise`` counts as a TOF of about zero — kept in the front's field
    (ranked at ~0), not excluded — while every non-noise class is a real
    negative rate that must not dominate or be dominated, so it leaves the
    front and lists below it, labelled. An absent record is not classified
    at all: the caller's existing gate keeps judging it."""
    if sign is None:
        return True
    return str(sign.get("class") or "") == "noise"

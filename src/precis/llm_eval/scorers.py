"""Golden-task scorers — a model's response → a 0..1 score → a 1..5 ordinal.

Slice 11 of the factory design. The eval harness (:mod:`.harness`) runs a
candidate model over a gold set (:mod:`.tasks`) and scores each response with
one of these. A scorer is a pure function of ``(response_text, response_data,
expect)`` returning a score in ``[0, 1]``; :func:`bucket_to_ordinal` maps the
per-axis mean onto the catalog's 1..5 ordinal (``llm_catalog.record_eval``).

Two axes score **deterministically** and ship now:

* ``needle`` (``long-context-recall``) — was the planted fact retrieved?
* ``tool_json`` (``tool-structured``) — did the structured answer match?

The heavy axes (``code`` = run the fix's tests, ``summarize-extract`` = rubric
judge, ``reasoning-convergence`` = prefer live telemetry) need a test-runner /
judge and are declared but not wired here — the harness logs them skipped
rather than silently dropping them (see :data:`SCORERS`).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

#: A scorer: ``(response_text, response_data, expect) -> score in [0, 1]``.
Scorer = Callable[[str, "dict[str, Any] | None", "dict[str, Any]"], float]


def _norm(s: str) -> str:
    """Casefold + collapse whitespace for lenient substring matching."""
    return re.sub(r"\s+", " ", s).strip().casefold()


def score_needle(
    response_text: str, response_data: dict[str, Any] | None, expect: dict[str, Any]
) -> float:
    """long-context-recall: 1.0 iff the planted needle appears in the response.

    ``expect = {"needle": "<the planted fact>"}``. Matching is
    whitespace-insensitive + casefolded so formatting noise around the fact
    doesn't fail an otherwise-correct retrieval. An ``expect.aliases`` list (any
    acceptable phrasing) also counts.
    """
    hay = _norm(response_text or "")
    needles = [str(expect.get("needle") or "")]
    needles += [str(a) for a in (expect.get("aliases") or [])]
    return 1.0 if any(n and _norm(n) in hay for n in needles) else 0.0


def score_tool_json(
    response_text: str, response_data: dict[str, Any] | None, expect: dict[str, Any]
) -> float:
    """tool-structured: fraction of expected answer keys matched in the output.

    ``expect = {"answer": {k: v, ...}}``. Reads the model's structured output
    (``LlmResult.data``, the parsed trailing JSON block) and scores the share of
    expected ``(key, value)`` pairs present with a matching (stringified,
    normalized) value. A response that produced no parseable structured object
    scores 0 — which is itself the tool-conformance signal.
    """
    answer = expect.get("answer")
    if not isinstance(answer, dict) or not answer:
        return 0.0
    got = response_data if isinstance(response_data, dict) else None
    if got is None:
        return 0.0
    hits = 0
    for k, v in answer.items():
        if k in got and _norm(str(got[k])) == _norm(str(v)):
            hits += 1
    return hits / len(answer)


#: Registry of the wired (deterministic) scorers. A gold task names one via its
#: ``scorer`` field; an unknown scorer is skipped by the harness (logged), never
#: silently scored 0.
SCORERS: dict[str, Scorer] = {
    "needle": score_needle,
    "tool_json": score_tool_json,
}


def bucket_to_ordinal(mean_score: float) -> int:
    """Map a per-axis mean score in ``[0, 1]`` onto the catalog's 1..5 ordinal.

    Linear: ``0.0 → 1`` (worst) … ``1.0 → 5`` (best), rounding to the nearest
    band. Clamped so a stray out-of-range mean can't produce an invalid ordinal
    (``record_eval`` rejects anything outside 1..5).
    """
    clamped = max(0.0, min(1.0, mean_score))
    return max(1, min(5, 1 + round(clamped * 4)))


__all__ = [
    "SCORERS",
    "Scorer",
    "bucket_to_ordinal",
    "score_needle",
    "score_tool_json",
]

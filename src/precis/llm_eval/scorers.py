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


def score_exact(
    response_text: str, response_data: dict[str, Any] | None, expect: dict[str, Any]
) -> float:
    """reasoning-convergence: 1.0 iff the model's final answer matches the gold.

    ``expect = {"answer": "<value>", "aliases": [...]}``. Tasks are asked to end
    with ``ANSWER: <x>``; when that marker is present only the tail is compared
    (equality after normalization, or numeric value so ``42`` == ``42.0``), which
    avoids the false positives of matching the gold digit anywhere in a chain of
    thought. With no marker, falls back to a substring test over the whole reply.
    """
    gold = [str(expect.get("answer") or "")]
    gold += [str(a) for a in (expect.get("aliases") or [])]
    text = response_text or ""
    m = re.search(r"answer\s*[:=]\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    tail = m.group(1) if m else text
    tail_n = _norm(tail)
    tail_num = re.sub(r"[^0-9.\-]", "", tail.splitlines()[0] if tail else "")
    for g in gold:
        gn = _norm(g)
        if not gn:
            continue
        if gn == tail_n or (m and gn in tail_n) or (not m and gn in tail_n):
            return 1.0
        try:
            if tail_num and abs(float(g) - float(tail_num)) < 1e-9:
                return 1.0
        except ValueError:
            pass
    return 0.0


def score_keypoints(
    response_text: str, response_data: dict[str, Any] | None, expect: dict[str, Any]
) -> float:
    """summarize-extract: fraction of required key facts a faithful summary keeps.

    ``expect = {"keypoints": ["distinctive token", ...], "forbid": [...]}``.
    Coverage = matched keypoints / total (each keypoint a distinctive name/number
    a faithful summary must carry). Any ``forbid`` phrase present — a fact the
    source does not support — zeroes the task (a hallucination check, not just
    recall).
    """
    hay = _norm(response_text or "")
    kps = [str(k) for k in (expect.get("keypoints") or [])]
    if not kps:
        return 0.0
    for bad in expect.get("forbid") or []:
        if _norm(str(bad)) and _norm(str(bad)) in hay:
            return 0.0
    hits = sum(1 for k in kps if _norm(k) and _norm(k) in hay)
    return hits / len(kps)


def _extract_code(text: str) -> str:
    """Pull the last fenced code block from a reply (whole reply if unfenced)."""
    blocks = re.findall(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
    return blocks[-1] if blocks else text


def score_code(
    response_text: str, response_data: dict[str, Any] | None, expect: dict[str, Any]
) -> float:
    """code: 1.0 iff the model's code passes the task's hidden test.

    ``expect = {"test": "<python asserting on the entrypoint>", "timeout": 10}``.
    Extracts the last fenced code block, appends the test, and runs it in an
    isolated (``python -I``) subprocess in a throwaway tempdir; exit 0 → pass.

    Executes model-generated code — a **controlled-eval** convenience only (the
    subprocess is isolated + timed but not otherwise jailed); never point this at
    untrusted gold sets on a host you care about.
    """
    import os
    import subprocess
    import sys
    import tempfile

    code = _extract_code(response_text or "")
    if not code.strip():
        return 0.0
    test = str(expect.get("test") or "")
    timeout = int(expect.get("timeout") or 10)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "cand.py")
        with open(path, "w") as fh:
            fh.write(code + "\n\n" + test + "\n")
        try:
            proc = subprocess.run(
                [sys.executable, "-I", path],
                cwd=d,
                capture_output=True,
                timeout=timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return 0.0
    return 1.0 if proc.returncode == 0 else 0.0


#: Registry of the wired (deterministic) scorers. A gold task names one via its
#: ``scorer`` field; an unknown scorer is skipped by the harness (logged), never
#: silently scored 0.
SCORERS: dict[str, Scorer] = {
    "needle": score_needle,
    "tool_json": score_tool_json,
    "exact": score_exact,
    "keypoints": score_keypoints,
    "code": score_code,
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
    "score_code",
    "score_exact",
    "score_keypoints",
    "score_needle",
    "score_tool_json",
]

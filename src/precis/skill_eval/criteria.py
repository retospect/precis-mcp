"""Deterministic verdict of a :class:`Transcript` against a :class:`Criterion`.

Every failed clause becomes one reason line, so a red case says *which*
expectation the skill failed to teach — not just that it failed.
"""

from __future__ import annotations

from dataclasses import dataclass

from precis.skill_eval.cases import Criterion, Transcript, value_matches


@dataclass(frozen=True, slots=True)
class Verdict:
    passed: bool
    reasons: tuple[str, ...] = ()


def check(criterion: Criterion, transcript: Transcript) -> Verdict:
    reasons: list[str] = []
    for pat in criterion.calls:
        if not any(pat.matches(c) for c in transcript.calls):
            reasons.append(f"expected call not made: {pat.render()}")
    for pat in criterion.forbid_calls:
        hits = [c for c in transcript.calls if pat.matches(c)]
        if hits:
            reasons.append(f"forbidden call made: {hits[0].render()}")
    if criterion.record is not None:
        if transcript.record is None:
            reasons.append("expected a record, none produced")
        elif not value_matches(criterion.record, transcript.record):
            reasons.append(
                f"record mismatch: expected {criterion.record!r}, "
                f"got {transcript.record!r}"
            )
    answer = transcript.answer.lower()
    for needle in criterion.forbid_text:
        if needle.lower() in answer:
            reasons.append(f"forbidden text in answer: {needle!r}")
    return Verdict(passed=not reasons, reasons=tuple(reasons))


__all__ = ["Verdict", "check"]

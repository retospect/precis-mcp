"""Taproot Phase-1 eval harness — the ``dedup_judge`` gate.

Runs :func:`~precis.taproot.canon.dedup_judge` over every pair in
``tests/fixtures/taproot/claim_pairs.jsonl`` and grades it against the
fixture's **collapsed** label (README in that directory):
``equivalent`` -> ``same`` · ``broader``/``narrower``/``orthogonal`` ->
``different`` · ``contradicts`` -> ``contradicts``.

Primary metric: **over-merge rate -> 0** — a predicted ``same`` where the
fixture says ``different`` is the dangerous error (a wrong merge fuses
distinct claims). Under-merge (predicted ``different`` where the fixture
says ``same``) is tallied but tolerated (taproot.md's "safe direction").

This module makes live model calls when run with the real
:func:`~precis.taproot.canon.dedup_judge` — it is a **validation harness the
builder runs deliberately**, not something the offline test gate executes
(see ``tests/test_taproot_eval_canon.py``, which is skipped without an
explicit opt-in).

CLI: ``python -m precis.taproot.eval_canon [fixture_path]``.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from precis.taproot.canon import Verdict, Verdict3, dedup_judge

#: The fixture's 5-relation labels collapsed onto the 3 verdicts v1 grades
#: (tests/fixtures/taproot/README.md "v1 grades collapsed"). Kept here (not
#: imported from the fixture) since it is the *grading contract*, not fixture
#: data — a future v2 restoring the hierarchy grades the same rows
#: differently without touching the fixture.
_LABEL_COLLAPSE: dict[str, Verdict3] = {
    "equivalent": "same",
    "broader": "different",
    "narrower": "different",
    "orthogonal": "different",
    "contradicts": "contradicts",
}

DispatchJudgeFn = Callable[[str, str], Verdict]


def collapse_label(relation: str) -> Verdict3:
    """Map a fixture's 5-relation ``relation`` onto the 3-verdict grading
    label. Raises :class:`ValueError` on an unrecognized relation — a typo
    or a new fixture label the harness doesn't know how to grade should
    fail loud, not silently miscount."""
    try:
        return _LABEL_COLLAPSE[relation]
    except KeyError:
        raise ValueError(f"unrecognized fixture relation: {relation!r}") from None


@dataclass
class PairResult:
    pair_id: int
    expected: Verdict3
    predicted: Verdict3
    confidence: float
    rationale: str


@dataclass
class Report:
    """The eval harness's output — confusion matrix + over/under-merge
    tallies, plus the individual pair results for drill-down."""

    results: list[PairResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def confusion(self) -> Counter[tuple[Verdict3, Verdict3]]:
        """``{(expected, predicted): count}`` — the 3x3 confusion matrix."""
        return Counter((r.expected, r.predicted) for r in self.results)

    @property
    def over_merges(self) -> list[PairResult]:
        """Predicted ``same`` where expected ``different`` — the dangerous
        error the fixture's bar is zero on."""
        return [
            r
            for r in self.results
            if r.expected == "different" and r.predicted == "same"
        ]

    @property
    def under_merges(self) -> list[PairResult]:
        """Predicted ``different`` where expected ``same`` — tolerated."""
        return [
            r
            for r in self.results
            if r.expected == "same" and r.predicted == "different"
        ]

    @property
    def over_merge_rate(self) -> float:
        return len(self.over_merges) / self.total if self.total else 0.0

    @property
    def under_merge_rate(self) -> float:
        return len(self.under_merges) / self.total if self.total else 0.0

    def format(self) -> str:
        verdicts: tuple[Verdict3, ...] = ("same", "different", "contradicts")
        conf = self.confusion
        lines = [
            f"Taproot canonicalization eval — {self.total} pairs",
            "",
            "Confusion (rows=expected, cols=predicted):",
            "              " + "  ".join(f"{v:>12}" for v in verdicts),
        ]
        for expected in verdicts:
            row = "  ".join(f"{conf.get((expected, p), 0):>12}" for p in verdicts)
            lines.append(f"  {expected:>10}  {row}")
        lines.extend(
            [
                "",
                f"over-merge  (same where different): {len(self.over_merges)} "
                f"({self.over_merge_rate:.1%}) — bar is 0",
                f"under-merge (different where same): {len(self.under_merges)} "
                f"({self.under_merge_rate:.1%}) — tolerated",
            ]
        )
        if self.over_merges:
            lines.append("")
            lines.append("Over-merges (investigate individually):")
            for r in self.over_merges:
                lines.append(
                    f"  pair {r.pair_id}: confidence={r.confidence:.2f} "
                    f"rationale={r.rationale!r}"
                )
        return "\n".join(lines)


def _load_pairs(fixture_path: str | Path) -> list[dict[str, Any]]:
    path = Path(fixture_path)
    pairs: list[dict[str, Any]] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            pairs.append(json.loads(line))
    return pairs


def eval_canonicalization(
    fixture_path: str | Path,
    *,
    dedup_judge_fn: DispatchJudgeFn = dedup_judge,
) -> Report:
    """Run ``dedup_judge_fn`` over every pair in ``fixture_path`` and grade
    against the collapsed label. Prints the report and returns it.

    ``dedup_judge_fn`` defaults to the real :func:`~precis.taproot.canon.dedup_judge`
    (a live MEDIUM-tier dispatch per pair) — inject a stub for an offline
    unit test.
    """
    results: list[PairResult] = []
    for row in _load_pairs(fixture_path):
        expected = collapse_label(row["relation"])
        verdict = dedup_judge_fn(row["claim_a"], row["claim_b"])
        results.append(
            PairResult(
                pair_id=int(row["pair_id"]),
                expected=expected,
                predicted=verdict["verdict"],
                confidence=verdict["confidence"],
                rationale=verdict["rationale"],
            )
        )
    report = Report(results=results)
    print(report.format())
    return report


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    fixture = (
        Path(args[0])
        if args
        else (
            Path(__file__).resolve().parents[3]
            / "tests"
            / "fixtures"
            / "taproot"
            / "claim_pairs.jsonl"
        )
    )
    report = eval_canonicalization(fixture)
    return 0 if not report.over_merges else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PairResult",
    "Report",
    "collapse_label",
    "eval_canonicalization",
]

"""Taproot Phase-1 eval harness — the ``dedup_judge`` gate, plus the
``extract_claim`` atomicity gate.

Runs :func:`~precis.taproot.canon.dedup_judge` over every pair in
``tests/fixtures/taproot/claim_pairs.jsonl`` and grades it against the
fixture's **collapsed** label (README in that directory):
``equivalent`` -> ``same`` · ``broader``/``narrower``/``orthogonal`` ->
``different`` · ``contradicts`` -> ``contradicts``.

Primary metric: **over-merge rate -> 0** — a predicted ``same`` where the
fixture says ``different`` is the dangerous error (a wrong merge fuses
distinct claims). Under-merge (predicted ``different`` where the fixture
says ``same``) is tallied but tolerated (taproot.md's "safe direction").

:func:`eval_extraction` is the sibling gate for the AIDA-Atomic constraint
added to :func:`~precis.taproot.canon.extract_claim`
(``docs/backlog/taproot-atomic-claims.md`` §AIDA): it runs the extractor
over ``tests/fixtures/taproot/extraction_passages.jsonl`` and checks two
hard gates (bar = 0) plus one soft metric — see its docstring.

Both harnesses make live model calls when run with the real canon
functions — they are **validation harnesses the builder runs
deliberately**, not something the offline test gate executes (see
``tests/test_taproot_eval_canon.py`` / the extraction sibling test, both
skipped without an explicit opt-in).

CLI: ``python -m precis.taproot.eval_canon [fixture_path]`` (dedup gate
only).
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from precis.taproot.canon import (
    ClaimExtraction,
    Verdict,
    Verdict3,
    dedup_judge,
    extract_claim,
)

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


def _load_jsonl(fixture_path: str | Path) -> list[dict[str, Any]]:
    """One-object-per-line reader shared by both fixtures (``#``-comment
    lines and blank lines skipped)."""
    path = Path(fixture_path)
    rows: list[dict[str, Any]] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            rows.append(json.loads(line))
    return rows


def _load_pairs(fixture_path: str | Path) -> list[dict[str, Any]]:
    return _load_jsonl(fixture_path)


def eval_canonicalization(
    fixture_path: str | Path,
    *,
    dedup_judge_fn: DispatchJudgeFn = dedup_judge,
    progress: bool = True,
) -> Report:
    """Run ``dedup_judge_fn`` over every pair in ``fixture_path`` and grade
    against the collapsed label. Prints the report to stdout and returns it.

    ``dedup_judge_fn`` defaults to the real :func:`~precis.taproot.canon.dedup_judge`
    (a live MEDIUM-tier dispatch per pair) — inject a stub for an offline
    unit test.

    ``progress`` streams one flushed line per pair to **stderr** as it is
    judged (an over-merge is flagged inline with ``⚠``), so a live 238-pair
    run is observable instead of a silent ~40-minute black box — and a run
    that dies partway still shows every pair it judged. stdout stays the
    clean final report. Silenced (``progress=False``) by the offline unit
    tests that inject a stub judge.
    """
    rows = _load_pairs(fixture_path)
    total = len(rows)
    results: list[PairResult] = []
    for i, row in enumerate(rows, start=1):
        expected = collapse_label(row["relation"])
        verdict = dedup_judge_fn(row["claim_a"], row["claim_b"])
        predicted = verdict["verdict"]
        results.append(
            PairResult(
                pair_id=int(row["pair_id"]),
                expected=expected,
                predicted=predicted,
                confidence=verdict["confidence"],
                rationale=verdict["rationale"],
            )
        )
        if progress:
            over = expected == "different" and predicted == "same"
            print(
                f"[{i}/{total}] pair {row['pair_id']} "
                f"exp={expected} got={predicted} "
                f"conf={verdict['confidence']:.2f}"
                f"{'  ⚠ OVER-MERGE' if over else ''}",
                file=sys.stderr,
                flush=True,
            )
    report = Report(results=results)
    print(report.format())
    return report


# ── eval_extraction — the AIDA-Atomic gate ──────────────────────────────

ExtractFn = Callable[[str], ClaimExtraction]

#: A cheap lexical tell that an emitted atom still bundles a second
#: predicate — the hard gate's heuristic
#: (``docs/backlog/taproot-atomic-claims.md`` §AIDA). Deliberately
#: imperfect: it flags candidates for a human to look at, not a semantic
#: parse. Excludes a bare " and " (a legitimate condition list like "at
#: 300 K and 1 atm" would false-positive on it) — the markers below are
#: the ones that read as clause-joining in practice.
_CONJUNCTION_MARKERS = (
    " as well as ",
    " in addition to ",
    " along with ",
    " while ",
    " whereas ",
    ", and ",
    ", which also ",
)


def _has_predicate_conjunction(sentence: str) -> bool:
    """True if ``sentence`` still contains one of :data:`_CONJUNCTION_MARKERS`
    — a candidate un-split atom."""
    lowered = f" {sentence.lower()} "
    return any(marker in lowered for marker in _CONJUNCTION_MARKERS)


@dataclass
class ExtractionResult:
    """One graded fixture row."""

    passage_id: int
    expected_atom_count: int
    actual_atom_count: int
    compound_without_atoms: bool
    conjunction_atoms: list[str] = field(default_factory=list)
    not_claim_texts: list[str] = field(default_factory=list)


@dataclass
class ExtractionReport:
    """:func:`eval_extraction`'s output — two hard gates (bar = 0) plus one
    soft metric, and the per-row results for drill-down."""

    results: list[ExtractionResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def atom_count_matches(self) -> int:
        return sum(
            1 for r in self.results if r.actual_atom_count == r.expected_atom_count
        )

    @property
    def atom_count_agreement_rate(self) -> float:
        """Soft metric — how often the extractor's atom count matches the
        fixture's expectation. Not a gate: a model that atomizes
        differently (more/fewer atoms) than the fixture's author but still
        keeps every atom AIDA-atomic is not wrong, just differently
        granular."""
        return self.atom_count_matches / self.total if self.total else 0.0

    @property
    def compound_without_atoms_violations(self) -> list[ExtractionResult]:
        """Hard gate — bar is 0. A compound with no surviving atoms means
        nothing groundable backs the bundle; :func:`~precis.taproot.canon
        ._coerce_extraction` should have folded it to ``None``. Checked
        here too as a backstop against a caller bypassing that helper."""
        return [r for r in self.results if r.compound_without_atoms]

    @property
    def conjunction_violations(self) -> list[ExtractionResult]:
        """Hard gate — bar is 0. An emitted atom still reads as bundling a
        second predicate (see :data:`_CONJUNCTION_MARKERS`)."""
        return [r for r in self.results if r.conjunction_atoms]

    def format(self) -> str:
        lines = [
            f"Taproot extraction (AIDA-Atomic) eval — {self.total} passages",
            "",
            f"atom-count agreement: {self.atom_count_matches}/{self.total} "
            f"({self.atom_count_agreement_rate:.1%}) — soft metric",
            f"compound-without-atoms: {len(self.compound_without_atoms_violations)} "
            "— hard gate, bar is 0",
            f"atoms with a residual conjunction: "
            f"{len(self.conjunction_violations)} — hard gate, bar is 0",
        ]
        if self.compound_without_atoms_violations:
            lines.append("")
            lines.append("Compound-without-atoms (investigate individually):")
            for r in self.compound_without_atoms_violations:
                lines.append(f"  passage {r.passage_id}")
        if self.conjunction_violations:
            lines.append("")
            lines.append("Residual-conjunction atoms (investigate individually):")
            for r in self.conjunction_violations:
                lines.append(f"  passage {r.passage_id}: {r.conjunction_atoms}")
        return "\n".join(lines)


def eval_extraction(
    fixture_path: str | Path,
    *,
    extract_fn: ExtractFn = extract_claim,
    progress: bool = True,
) -> ExtractionReport:
    """Run ``extract_fn`` over every row in ``fixture_path`` and grade the
    AIDA-Atomic gate. Prints the report to stdout and returns it.

    Fixture row shape: ``id``, ``passage``, ``expected_atom_count`` (0 for
    a NO-CLAIM passage), optionally ``expected_not_claims`` (a list of
    rejected-conjunct substrings, kept for audit — not graded as a gate in
    v1) and ``note`` (provenance / rationale).

    ``extract_fn`` defaults to the real
    :func:`~precis.taproot.canon.extract_claim` (a live SMALL-tier dispatch
    per row) — inject a stub for an offline unit test.

    ``progress`` mirrors :func:`eval_canonicalization`'s streamed-to-stderr
    per-row line, flagging a hard-gate violation inline.
    """
    rows = _load_jsonl(fixture_path)
    total = len(rows)
    results: list[ExtractionResult] = []
    for i, row in enumerate(rows, start=1):
        extraction = extract_fn(row["passage"])
        conjunction_atoms = [
            atom.sentence
            for atom in extraction.atoms
            if _has_predicate_conjunction(atom.sentence)
        ]
        compound_without_atoms = (
            extraction.compound is not None and not extraction.atoms
        )
        result = ExtractionResult(
            passage_id=int(row["id"]),
            expected_atom_count=int(row["expected_atom_count"]),
            actual_atom_count=len(extraction.atoms),
            compound_without_atoms=compound_without_atoms,
            conjunction_atoms=conjunction_atoms,
            not_claim_texts=[nc["text"] for nc in extraction.not_claims],
        )
        results.append(result)
        if progress:
            flags = ""
            if compound_without_atoms:
                flags += "  ⚠ COMPOUND-WITHOUT-ATOMS"
            if conjunction_atoms:
                flags += "  ⚠ RESIDUAL-CONJUNCTION"
            print(
                f"[{i}/{total}] passage {result.passage_id} "
                f"expected_atoms={result.expected_atom_count} "
                f"got_atoms={result.actual_atom_count}{flags}",
                file=sys.stderr,
                flush=True,
            )
    report = ExtractionReport(results=results)
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
    "ExtractFn",
    "ExtractionReport",
    "ExtractionResult",
    "PairResult",
    "Report",
    "collapse_label",
    "eval_canonicalization",
    "eval_extraction",
]

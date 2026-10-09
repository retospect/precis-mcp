"""``python -m precis.skill_eval`` — the ``scripts/skill-eval`` entry point.

Exit 0 on all-pass or a skipped run (advisory: an unauthenticated host is
not a red), 1 when any case failed or errored, 2 on a malformed corpus or
bad usage.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from precis.errors import BadInput
from precis.skill_eval.cases import load_cases
from precis.skill_eval.report import DEFAULT_REPORT_PATH, run_cases
from precis.skill_eval.runners import FakeRunner, LiveClaudeRunner, Runner

SKILLS_DIR = Path("src/precis/data/skills")


def changed_skills(commit: str) -> set[str]:
    """Slugs of skill files touched by ``commit^..commit`` (``--diff``)."""
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{commit}^", commit, "--", str(SKILLS_DIR)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    return {Path(p).stem for p in out.split() if p.endswith(".md")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="skill-eval", description=__doc__)
    ap.add_argument("--runner", choices=("fake", "live"), default="fake")
    ap.add_argument("--cases", help="cases dir (default: the shipped corpus)")
    ap.add_argument("--skill", action="append", help="only this skill's cases")
    ap.add_argument(
        "--diff",
        nargs="?",
        const="HEAD",
        metavar="COMMIT",
        help="only skills whose file changed in COMMIT^..COMMIT (default HEAD)",
    )
    ap.add_argument("--out", default=DEFAULT_REPORT_PATH, help="JSON report path")
    ap.add_argument("--model", help="live: model override")
    ap.add_argument("--case-usd", type=float, help="live: per-case USD cap")
    ap.add_argument("--run-usd", type=float, help="live: per-run USD cap")
    ap.add_argument("--timeout-s", type=float, help="live: per-case wall clock")
    ns = ap.parse_args(argv)

    skills: set[str] | None = set(ns.skill) if ns.skill else None
    if ns.diff:
        touched = changed_skills(ns.diff)
        skills = touched if skills is None else skills & touched
    try:
        cases = load_cases(ns.cases, skills=skills)
    except BadInput as exc:
        print(f"skill-eval: {exc}", file=sys.stderr)
        return 2
    runner: Runner
    if ns.runner == "live":
        runner = LiveClaudeRunner(
            model=ns.model,
            case_usd=ns.case_usd,
            run_usd=ns.run_usd,
            timeout_s=ns.timeout_s,
        )
    else:
        runner = FakeRunner()
    report = run_cases(cases, runner)
    path = report.write_json(ns.out)
    print(report.render_table())
    print(f"report: {path}")
    return 1 if report.red else 0


if __name__ == "__main__":
    sys.exit(main())

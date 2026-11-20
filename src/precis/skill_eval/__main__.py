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
from precis.skill_eval.cases import load_cases, load_discovery_cases
from precis.skill_eval.report import (
    DEFAULT_REPORT_PATH,
    Report,
    classify_pairs,
    run_cases,
)
from precis.skill_eval.runners import (
    ExecutedClaudeRunner,
    FakeRunner,
    LiveClaudeRunner,
    Runner,
)

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
    ap.add_argument("--runner", choices=("fake", "live", "executed"), default="fake")
    ap.add_argument(
        "--execute",
        action="store_true",
        help="shortcut for --runner executed: run the corpus through "
        "claude -p with a precis MCP bound to the eval DSN",
    )
    ap.add_argument("--cases", help="cases dir (default: the shipped corpus)")
    ap.add_argument(
        "--corpus",
        choices=("supplied", "discovery"),
        help="corpus format; defaults to discovery for discovery/paired conditions",
    )
    ap.add_argument("--skill", action="append", help="only this skill's cases")
    ap.add_argument(
        "--diff",
        nargs="?",
        const="HEAD",
        metavar="COMMIT",
        help="only skills whose file changed in COMMIT^..COMMIT (default HEAD)",
    )
    ap.add_argument("--out", default=DEFAULT_REPORT_PATH, help="JSON report path")
    ap.add_argument(
        "--model",
        action="append",
        help="model override (repeatable on executed: one corpus pass per "
        "model, accuracy-by-depth reported per model)",
    )
    ap.add_argument("--case-usd", type=float, help="live/executed: per-case USD cap")
    ap.add_argument("--run-usd", type=float, help="live/executed: per-run USD cap")
    ap.add_argument(
        "--timeout-s", type=float, help="live/executed: per-case wall clock"
    )
    ap.add_argument(
        "--dsn",
        help="executed: the eval database DSN (env PRECIS_SKILL_EVAL_DSN; "
        "default scripts/dev's precis_dev) — never prod",
    )
    ap.add_argument(
        "--serve-cmd",
        help="executed: how the spawned claude launches precis serve "
        "(env PRECIS_SKILL_EVAL_SERVE_CMD; default 'precis serve')",
    )
    ap.add_argument(
        "--max-turns", type=int, help="executed: per-case turn cap (default 30)"
    )
    ap.add_argument(
        "--condition",
        choices=("supplied", "discovery", "paired"),
        default="supplied",
        help="how prompts are built: 'supplied' injects the skill text (default); "
        "'discovery' renders no skill text and no slug — the model starts from "
        "the tool surface alone; 'paired' runs each case under both (executed "
        "only) and classifies each failed pair (gr634773)",
    )
    ns = ap.parse_args(argv)

    if ns.execute and ns.runner not in ("fake", "executed"):
        print("skill-eval: --execute conflicts with --runner live", file=sys.stderr)
        return 2
    runner_name = "executed" if ns.execute else ns.runner
    if ns.condition == "paired" and runner_name != "executed":
        print(
            "skill-eval: --condition paired needs --runner executed",
            file=sys.stderr,
        )
        return 2

    skills: set[str] | None = set(ns.skill) if ns.skill else None
    if ns.diff:
        touched = changed_skills(ns.diff)
        skills = touched if skills is None else skills & touched
    try:
        corpus = ns.corpus or (
            "supplied" if ns.condition == "supplied" else "discovery"
        )
        if corpus == "discovery":
            cases = load_discovery_cases(ns.cases)
            if skills is not None:
                cases = [case for case in cases if case.skill in skills]
        else:
            cases = load_cases(ns.cases, skills=skills)
    except BadInput as exc:
        print(f"skill-eval: {exc}", file=sys.stderr)
        return 2

    if runner_name != "executed":
        models = ns.model or [None]
        if len(models) > 1:
            print("skill-eval: repeated --model is executed-only", file=sys.stderr)
            return 2
        runner: Runner
        if runner_name == "live":
            runner = LiveClaudeRunner(
                model=models[0],
                case_usd=ns.case_usd,
                run_usd=ns.run_usd,
                timeout_s=ns.timeout_s,
                condition=ns.condition,
            )
        else:
            runner = FakeRunner(condition=ns.condition)
        report = run_cases(cases, runner)
    else:
        # One corpus pass per model; case rows carry the model so the
        # report's accuracy-by-depth table splits them (gr608716). Each
        # condition gets its own runner with the same caps: paired holds
        # model, task, environment and budget constant across conditions
        # (gr634773).
        conditions: tuple[str, ...] = (
            ("supplied", "discovery") if ns.condition == "paired" else (ns.condition,)
        )
        report = Report(runner="executed", condition=ns.condition)
        skips: list[str] = []
        for model in ns.model or [None]:
            for condition in conditions:
                runner = ExecutedClaudeRunner(
                    dsn=ns.dsn,
                    model=model,
                    case_usd=ns.case_usd,
                    run_usd=ns.run_usd,
                    timeout_s=ns.timeout_s,
                    max_turns=ns.max_turns,
                    serve_cmd=ns.serve_cmd,
                    condition=condition,
                )
                r = run_cases(cases, runner)
                if r.status == "skipped":
                    skips.append(f"{model or 'default'}/{condition}: {r.reason}")
                    continue
                report.cases.extend(r.cases)
                report.spent_usd += r.spent_usd
        if skips and not report.cases:
            report.status = "skipped"
            report.reason = "; ".join(skips)
        elif skips:
            print(
                f"skill-eval: skipped condition pass(es): {'; '.join(skips)}",
                file=sys.stderr,
            )
        if ns.condition == "paired" and report.cases:
            classify_pairs(report.cases)
    path = report.write_json(ns.out)
    print(report.render_table())
    print(f"report: {path}")
    return 1 if report.red else 0


if __name__ == "__main__":
    sys.exit(main())

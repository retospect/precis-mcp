"""The advisory recurring sampler for the discovery condition (gr634773).

``scripts/skill-discovery-review`` runs this. One pass:

1. **Select** at most ``--limit`` discovery cases — every case whose
   skill's file changed since the last review stamp first, then every
   case that failed in a retained report, then the rest ordered by
   fewest retained discovery runs per family (the under-tested tail).
2. **Run** each selected case *paired* — one executed pass with the
   skill text supplied, one with no skill text and no slug — both under
   the same model, environment and budget, against the scratch DSN.
3. **Retain** the full report under
   ``~/.cache/precis/skill-discovery/<ts>.json``: model, code and
   skills revision, conditions, budgets, every case row (its transcript
   calls, answer, discovery metrics and verdict) and one repair brief
   per failed pair.

``--rerun-nearby`` instead reruns the failing cases from the newest
retained report plus nearby cases in the same families, capped by ``--limit``.

The sampler is advisory, never a ship gate, and it proposes repairs —
it never patches a skill or ships anything. A missing/unauthenticated
``claude`` or a test stack that can't serve skips the pass (exit 0),
same standing as ``scripts/skill-eval``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Collection, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from precis.skill_eval.cases import EvalCase, load_discovery_cases
from precis.skill_eval.report import (
    CaseResult,
    Report,
    classify_failure,
    classify_pairs,
    run_cases,
)
from precis.skill_eval.runners import ExecutedClaudeRunner

#: Where retained review reports live — the selector's "previously
#: failed" input and the sampler's memory across runs.
CACHE_DIR = Path("~/.cache/precis/skill-discovery").expanduser()

#: The cadence log the wrapper stamps; its newest dated line bounds the
#: changed-skills diff (same clock as ``scripts/cadence-lib.sh``).
STAMP_LOG = Path("~/.cache/precis/skill-discovery-review.log").expanduser()

#: The shipped skill corpus the changed-skills diff reads.
SKILLS_DIR = "src/precis/data/skills"

#: Wall-clock bound for local git history and revision lookups.
GIT_TIMEOUT_S = 30

#: The two halves of a paired run, in report order.
CONDITIONS = ("supplied", "discovery")

#: Default sample size — small on purpose (risk:spend): every selected
#: case runs twice (once per condition).
DEFAULT_LIMIT = 3


def select_review_cases(
    cases: Iterable[EvalCase],
    *,
    changed_skills: Collection[str] = (),
    failed_case_ids: Collection[str] = (),
    limit: int,
    run_counts: Mapping[str, int] | None = None,
) -> list[EvalCase]:
    """At most ``limit`` cases, in sampler priority order (gr634773):
    every changed-skill case first, then every previously-failed case,
    then the rest ordered by fewest retained discovery runs per family (the under-tested
    tail), name-broken for determinism. A case already picked by an
    earlier tier never repeats."""
    if limit < 1:
        return []
    changed = set(changed_skills)
    failed = set(failed_case_ids)
    pending = list(cases)
    picked: list[EvalCase] = []
    seen: set[str] = set()

    def take(tier: Iterable[EvalCase]) -> None:
        for case in tier:
            if len(picked) >= limit or case.case_id in seen:
                continue
            picked.append(case)
            seen.add(case.case_id)

    take(c for c in pending if c.skill in changed)
    take(c for c in pending if c.case_id in failed)
    coverage = run_counts or {}
    counts: dict[str, int] = {}
    for c in pending:
        counts[c.skill] = counts.get(c.skill, 0) + coverage.get(c.case_id, 0)
    rest = sorted(
        (c for c in pending if c.case_id not in seen),
        key=lambda c: (counts[c.skill], coverage.get(c.case_id, 0), c.skill, c.case_id),
    )
    take(rest)
    return picked


def discovery_history(paths: Iterable[Path]) -> tuple[set[str], dict[str, int]]:
    """Latest discovery verdict per case/model and actual discovery run counts.

    Timestamped report filenames order observations; supplied and skipped rows
    neither clear discovery failures nor count as discovery coverage.
    """
    latest: dict[tuple[str, str], str] = {}
    counts: dict[str, int] = {}
    for path in sorted(paths):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        report = data.get("report", data)
        rows = report.get("cases") if isinstance(report, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or row.get("condition") != "discovery":
                continue
            case_id = row.get("case_id")
            status = row.get("status")
            if not isinstance(case_id, str) or status not in ("pass", "fail", "error"):
                continue
            latest[(case_id, str(row.get("model")))] = status
            counts[case_id] = counts.get(case_id, 0) + 1
    return {
        case_id
        for (case_id, _), status in latest.items()
        if status in ("fail", "error")
    }, counts


def failed_case_ids_from_reports(paths: Iterable[Path]) -> set[str]:
    """Cases whose latest discovery verdict still fails (per model)."""
    return discovery_history(paths)[0]


def latest_stamp(log_path: Path = STAMP_LOG) -> str | None:
    """Newest ``YYYY-MM-DD`` in the cadence log, if any — the lines are
    ``**YYYY-MM-DD** — <note>``, so the date is extracted by regex rather
    than whitespace splitting."""
    try:
        text = log_path.read_text(encoding="utf-8")
    except OSError:
        return None
    dates = [m.group(0) for m in re.finditer(r"\d{4}-\d{2}-\d{2}", text)]
    return max(dates) if dates else None


def changed_skills_since(date_str: str, *, repo: Path = Path(".")) -> set[str]:
    """Skill slugs whose file changed since ``date_str`` (git log)."""
    out = subprocess.run(
        [
            "git",
            "log",
            "--since",
            date_str,
            "--format=",
            "--name-only",
            "--",
            SKILLS_DIR,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=GIT_TIMEOUT_S,
    ).stdout
    return {Path(line).stem for line in out.splitlines() if line.endswith(".md")}


def _revision(spec: str, *, repo: Path = Path(".")) -> str | None:
    """Resolve a git revision, or return ``None`` if the lookup fails."""
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", spec],
                cwd=repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
                timeout=GIT_TIMEOUT_S,
            ).stdout.strip()
            or None
        )
    except (OSError, subprocess.SubprocessError):
        return None


def repair_briefs(rows: list[CaseResult]) -> list[str]:
    """One repair brief per failed supplied/discovery pair — a proposal
    for a human-led repair, classified so the right layer gets the fix."""
    pairs: dict[tuple[str | None, str], dict[str, CaseResult]] = {}
    for row in rows:
        pairs.setdefault((row.model, row.case_id), {})[row.condition] = row
    out: list[str] = []
    for (model, case_id), conds in sorted(
        pairs.items(), key=lambda kv: (kv[0][1], kv[0][0] or "")
    ):
        supplied = conds.get("supplied")
        discovery = conds.get("discovery")
        if supplied is None or discovery is None:
            continue
        if supplied.status == "pass" and discovery.status == "pass":
            continue
        cls = classify_failure(supplied, discovery)
        probe = (
            f"discovery took {discovery.discovery_calls} calls / "
            f"{discovery.discovery_bytes} result bytes before the first correct call"
        )
        why = "; ".join(discovery.reasons) or "; ".join(supplied.reasons)
        direction = {
            "retrieval": (
                "passes with the text in hand, so the agent never found it — "
                f"propose better search bits / entry points for {supplied.skill}"
            ),
            "guidance": (
                f"fails even with {supplied.skill} supplied and clean calls — "
                "propose a focused fix to what the guidance teaches"
            ),
            "tool": (
                "precis calls errored or the runner crashed — check the "
                "handler/tool surface, not the prose"
            ),
            "unattributed": (
                "the pair is contradictory or partial — rerun it before "
                "proposing any repair"
            ),
        }[cls]
        out.append(
            f"[{cls}] {case_id} ({model or 'default'}): "
            f"supplied={supplied.status} discovery={discovery.status}; "
            f"{probe}; {direction}." + (f" Discovery said: {why}." if why else "")
        )
    return out


def _retained_report(
    report: Report,
    *,
    models: list[str | None],
    budgets: dict[str, Any],
    briefs: list[str],
) -> dict[str, Any]:
    """The retained-pass payload: everything a later selector or audit
    needs to explain this sample — model, code and skills revision,
    conditions, budgets, the per-case rows (transcripts included) and
    the repair briefs."""
    return {
        "minted_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": [m or "default" for m in models],
        "code_revision": _revision("HEAD"),
        "skills_revision": _revision(f"HEAD:{SKILLS_DIR}"),
        "conditions": list(CONDITIONS),
        "budgets": budgets,
        "report": report.to_dict(),
        "briefs": briefs,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="skill-discovery-review", description=__doc__)
    ap.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="max cases to sample (default %(default)s; each runs twice — "
        "once per condition)",
    )
    ap.add_argument(
        "--rerun-nearby",
        action="store_true",
        help="rerun the failing cases from the newest retained report plus "
        "nearby cases in the same families (still capped by --limit)",
    )
    ap.add_argument(
        "--out-dir",
        default=str(CACHE_DIR),
        help="where retained reports live (default ~/.cache/precis/skill-discovery)",
    )
    ap.add_argument(
        "--cases",
        help="discovery cases dir (default: the shipped discovery corpus)",
    )
    ap.add_argument(
        "--since",
        help="override the stamp date for the changed-skills diff "
        "(default: newest dated line of the cadence log)",
    )
    ap.add_argument("--model", action="append", help="model override (repeatable)")
    ap.add_argument("--case-usd", type=float, help="per-case USD cap")
    ap.add_argument("--run-usd", type=float, help="per-condition USD cap")
    ap.add_argument("--timeout-s", type=float, help="per-case wall clock")
    ap.add_argument("--max-turns", type=int, help="per-case turn cap")
    ap.add_argument("--dsn", help="the eval database DSN — never prod")
    ap.add_argument("--serve-cmd", help="how claude launches precis serve")
    ns = ap.parse_args(argv)

    try:
        cases = load_discovery_cases(ns.cases)
    except Exception as exc:
        print(f"skill-discovery-review: {exc}", file=sys.stderr)
        return 2
    out_dir = Path(ns.out_dir).expanduser()
    retained = sorted(out_dir.glob("*.json"))

    if ns.rerun_nearby:
        failed = failed_case_ids_from_reports(retained[-1:])
        families = {c.skill for c in cases if c.case_id in failed}
        selected = select_review_cases(
            [c for c in cases if c.skill in families],
            failed_case_ids=failed,
            limit=ns.limit,
            run_counts=discovery_history(retained)[1],
        )
        if not selected:
            print(
                "skill-discovery-review: --rerun-nearby: no failures in "
                f"the newest retained report ({out_dir})"
            )
            return 0
    else:
        stamp = ns.since or latest_stamp()
        try:
            changed = changed_skills_since(stamp) if stamp else set()
        except (OSError, subprocess.SubprocessError) as exc:
            print(
                f"skill-discovery-review: changed-skills diff failed: {exc}",
                file=sys.stderr,
            )
            return 2
        failed, coverage = discovery_history(retained)
        selected = select_review_cases(
            cases,
            changed_skills=changed,
            failed_case_ids=failed,
            limit=ns.limit,
            run_counts=coverage,
        )
        if not selected:
            print("skill-discovery-review: no cases selected")
            return 0

    report = Report(runner="executed", condition="paired")
    skips: list[str] = []
    for model in ns.model or [None]:
        for condition in CONDITIONS:
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
            r = run_cases(selected, runner)
            if r.status == "skipped":
                skips.append(f"{model or 'default'}/{condition}: {r.reason}")
                continue
            report.cases.extend(r.cases)
            report.spent_usd += r.spent_usd
    if not report.cases:
        print(f"skill-discovery-review: skipped — {'; '.join(skips)}")
        return 0
    if skips:
        print(
            f"skill-discovery-review: skipped condition pass(es): {'; '.join(skips)}",
            file=sys.stderr,
        )
    classify_pairs(report.cases)
    briefs = repair_briefs(report.cases)
    payload = _retained_report(
        report,
        models=ns.model or [None],
        budgets={
            "case_usd": runner.case_usd,
            "run_usd": runner.run_usd,
            "timeout_s": runner.timeout_s,
            "max_turns": runner.max_turns,
            "limit": ns.limit,
        },
        briefs=briefs,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{stamp}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(report.render_table())
    if briefs:
        print("repair briefs:")
        for brief in briefs:
            print(f"  {brief}")
    else:
        print("repair briefs: none — every pair passed or stayed unattributed")
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "CACHE_DIR",
    "CONDITIONS",
    "DEFAULT_LIMIT",
    "STAMP_LOG",
    "changed_skills_since",
    "failed_case_ids_from_reports",
    "latest_stamp",
    "main",
    "repair_briefs",
    "select_review_cases",
]

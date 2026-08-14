"""``precis taproot-migrate {score,dry-run}`` — the migration runner for
existing (pre-decomposition) claim hubs, Phase 0 + Phase 1 of
``docs/backlog/taproot-atomic-claims.md``'s Strategy. Both subcommands are
strictly read-only — a thin CLI skin over :mod:`precis.taproot.migrate`.

    precis taproot-migrate score
    precis taproot-migrate score --format json
    precis taproot-migrate dry-run --limit 50
    precis taproot-migrate dry-run --limit 50 --cohort likely-compound --controls 10
    precis taproot-migrate dry-run --limit 50 --out /tmp/report.md

Phase 2 (apply, the quiet-window write) and Phase 3 (human review) are not
built here — see the build ticket's Strategy section.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from precis.cli._common import resolve_dsn


def add_parser(subparsers: Any) -> None:
    """Register the ``taproot-migrate`` subcommand group (``score`` /
    ``dry-run``)."""
    p = subparsers.add_parser(
        "taproot-migrate",
        help="Taproot atomic-claims migration runner — phase 0 (score/cohort) "
        "+ phase 1 (dry-run decomposition) over EXISTING claim hubs. Both "
        "subcommands are read-only.",
    )
    tsub = p.add_subparsers(dest="taproot_migrate_cmd", required=True)

    s = tsub.add_parser(
        "score",
        help="Phase 0: score+cohort every live, not-yet-migrated claim hub "
        "by title compoundness heuristics (conjunctions/length/punctuation). "
        "No model call, read-only.",
    )
    s.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    s.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")

    d = tsub.add_parser(
        "dry-run",
        help="Phase 1: run extract_claim over the top-scored hubs' claim "
        "sentences and report proposed splits as markdown. Writes NOTHING "
        "(no refs/links/meta/chunks) -- LLM spend only.",
    )
    d.add_argument(
        "--limit",
        type=int,
        required=True,
        help="Number of top-scored hubs to evaluate.",
    )
    d.add_argument(
        "--cohort",
        choices=("likely-compound", "uncertain", "likely-atomic"),
        default=None,
        help="Restrict the top-scored set to one cohort (default: all cohorts).",
    )
    d.add_argument(
        "--controls",
        type=int,
        default=0,
        help="Also sample this many hubs from the bottom of the likely-atomic "
        "cohort, as a pass-through sanity control (default: 0).",
    )
    d.add_argument(
        "--out",
        default=None,
        help="Write the markdown report to this file instead of stdout.",
    )
    d.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")


def _run_score(args: argparse.Namespace) -> None:
    from precis.store import Store
    from precis.taproot.migrate import COHORTS, score_hubs

    store = Store.connect(resolve_dsn(args.database_url))
    try:
        scores = score_hubs(store)
    finally:
        store.close()

    counts: dict[str, int] = {}
    for s in scores:
        counts[s.cohort] = counts.get(s.cohort, 0) + 1

    if args.format == "json":
        print(
            json.dumps(
                {
                    "total": len(scores),
                    "cohorts": {c: counts.get(c, 0) for c in COHORTS},
                    "top": [
                        {
                            "ref_id": s.ref_id,
                            "title": s.title,
                            "score": s.score,
                            "cohort": s.cohort,
                            "signals": list(s.signals),
                        }
                        for s in scores[:30]
                    ],
                },
                indent=2,
            )
        )
        return

    print(f"taproot-migrate score: {len(scores)} live, not-yet-migrated claim hub(s)")
    for cohort in COHORTS:
        print(f"  {cohort}: {counts.get(cohort, 0)}")
    if not scores:
        return
    print()
    print("Top 30 by score:")
    for s in scores[:30]:
        title = s.title if len(s.title) <= 70 else s.title[:67] + "..."
        signals = ",".join(s.signals) or "-"
        print(f"  fi{s.ref_id:<8} score={s.score}  {s.cohort:<15} [{signals}]  {title}")


def _run_dry_run(args: argparse.Namespace) -> None:
    from precis.store import Store
    from precis.taproot.migrate import dry_run, render_report

    store = Store.connect(resolve_dsn(args.database_url))
    try:
        report = dry_run(
            store,
            limit=args.limit,
            cohort=args.cohort,
            controls=args.controls,
        )
    finally:
        store.close()

    rendered = render_report(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(rendered)
        print(f"wrote {len(report.outcomes)} hub outcome(s) to {args.out}")
    else:
        print(rendered)


def run(args: argparse.Namespace) -> None:
    """Execute ``precis taproot-migrate <taproot_migrate_cmd>``."""
    if args.taproot_migrate_cmd == "score":
        _run_score(args)
    elif args.taproot_migrate_cmd == "dry-run":
        _run_dry_run(args)
    else:
        print(
            f"taproot-migrate: unknown subcommand {args.taproot_migrate_cmd!r}",
            file=sys.stderr,
        )
        sys.exit(2)


__all__ = ["add_parser", "run"]

"""``precis taproot-migrate {score,dry-run,canary}`` — the migration
runner for existing (pre-decomposition) claim hubs, Phase 0 + Phase 1 of
``docs/backlog/taproot-atomic-claims.md``'s Strategy. All subcommands make
zero claim-data writes (no refs/links/meta/chunks) — a thin CLI skin over
:mod:`precis.taproot.migrate` / :mod:`precis.taproot.eval_canon`.
``dry-run`` and ``canary`` bind their store to :mod:`precis.budget.meter`
so their real LLM dispatch resolves the host's serving endpoint and is
budget-metered, which writes ``llm_call_log`` telemetry (never claim
data); ``score`` makes no LLM calls at all.

The extractor tier defaults to **BIG** (round 2 of ``docs/backlog/
taproot-migration-extraction-quality-gates.md``): the labelled-25 A/B
re-run showed the SMALL tier collapsing multi-clause sentences to single
truncated atoms — every SMALL run now needs an explicit ``--tier small``.

Run ``canary`` (11 hand-authored passages through the chosen tier + the
migration gates, exit 1 on failure) before any bulk ``dry-run`` — it
catches a collapsed/degraded extractor for the cost of 11 calls instead
of a burned bulk run.

    precis taproot-migrate score
    precis taproot-migrate score --format json
    precis taproot-migrate canary
    precis taproot-migrate dry-run --limit 50
    precis taproot-migrate dry-run --limit 50 --cohort likely-compound --controls 10
    precis taproot-migrate dry-run --limit 50 --out /tmp/report.md
    precis taproot-migrate dry-run --limit 50 --json /tmp/report.jsonl \\
        --tier small --escalate

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
        "subcommands make zero claim-data writes.",
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

    c = tsub.add_parser(
        "canary",
        help="Pre-run extractor smoke test: 11 hand-authored passages "
        "through the chosen tier + the migration gates; exit 1 on any "
        "lossy/nested verdict or no-claim mismatch. Run before any bulk "
        "dry-run.",
    )
    c.add_argument(
        "--tier",
        choices=("small", "big"),
        default="big",
        help="Extractor tier to smoke-test (default: big — the dry-run default).",
    )
    c.add_argument(
        "--fixture",
        default=None,
        help="Override the packaged extraction_passages.jsonl fixture path.",
    )
    c.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")

    d = tsub.add_parser(
        "dry-run",
        help="Phase 1: run the strict extractor (BIG tier by default) over "
        "the top-scored hubs' claim sentences and report proposed splits "
        "as markdown. Zero claim-data writes (no refs/links/meta/chunks) "
        "-- LLM spend, budget-metered, only.",
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
    d.add_argument(
        "--json",
        default=None,
        help="Also write one JSON object per outcome (hub, score, verdict, "
        "gate metadata, full extraction, escalation results) to this file.",
    )
    d.add_argument(
        "--tier",
        choices=("small", "big"),
        default="big",
        help="Primary extractor tier (default: big — the A/B re-run showed "
        "SMALL collapsing multi-clause sentences to single truncated atoms).",
    )
    d.add_argument(
        "--escalate",
        action="store_true",
        help="With --tier small only: re-extract lossy/nested/junk-candidate "
        "outcomes with the BIG-tier extractor and record both results (P2-10).",
    )
    d.add_argument(
        "--control-seed",
        type=int,
        default=0,
        help="Seed for the uniform random control sample (default: 0, deterministic).",
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


def _resolve_extract_fn(tier: str) -> Any:
    """The strict extractor for a ``--tier`` value. Strict on purpose —
    both callers (dry-run, canary) must tell a dead dispatch apart from a
    semantic NO-CLAIM."""
    from precis.taproot.canon import extract_claim_strict, extract_claim_strict_big

    return extract_claim_strict_big if tier == "big" else extract_claim_strict


def _run_canary(args: argparse.Namespace) -> None:
    from precis.budget import meter
    from precis.store import Store
    from precis.taproot.eval_canon import (
        EXTRACTION_PASSAGES_FIXTURE,
        canary_extraction,
    )

    store = Store.connect(resolve_dsn(args.database_url))
    meter.bind_store(store)
    try:
        report = canary_extraction(
            args.fixture or EXTRACTION_PASSAGES_FIXTURE,
            extract_fn=_resolve_extract_fn(args.tier),
        )
    finally:
        store.close()
    if not report.ok:
        sys.exit(1)


def _run_dry_run(args: argparse.Namespace) -> None:
    from precis.store import Store
    from precis.taproot.migrate import dry_run, dump_outcomes_jsonl, render_report

    escalate_fn = None
    if args.escalate:
        if args.tier == "big":
            print(
                "taproot-migrate: --escalate is the SMALL→BIG retry; with "
                "--tier big the primary already runs BIG",
                file=sys.stderr,
            )
            sys.exit(2)
        from precis.taproot.canon import extract_claim_strict_big

        escalate_fn = extract_claim_strict_big

    store = Store.connect(resolve_dsn(args.database_url))
    # Bind the store to the budget meter so LOCAL dispatch can resolve the
    # host's real served_by llama-swap endpoint from the DB (instead of
    # falling to the dead 127.0.0.1:4000 default) and this run's LLM spend
    # is gated by the budget breaker. Writes telemetry only (llm_call_log +
    # transient serving-slot rows), never claim data.
    from precis.budget import meter

    meter.bind_store(store)
    try:
        report = dry_run(
            store,
            limit=args.limit,
            cohort=args.cohort,
            controls=args.controls,
            control_seed=args.control_seed,
            extract_fn=_resolve_extract_fn(args.tier),
            escalate_fn=escalate_fn,
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

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            f.write(dump_outcomes_jsonl(report))
        print(f"wrote {len(report.outcomes)} outcome(s) as JSONL to {args.json}")


def run(args: argparse.Namespace) -> None:
    """Execute ``precis taproot-migrate <taproot_migrate_cmd>``."""
    if args.taproot_migrate_cmd == "score":
        _run_score(args)
    elif args.taproot_migrate_cmd == "canary":
        _run_canary(args)
    elif args.taproot_migrate_cmd == "dry-run":
        _run_dry_run(args)
    else:
        print(
            f"taproot-migrate: unknown subcommand {args.taproot_migrate_cmd!r}",
            file=sys.stderr,
        )
        sys.exit(2)


__all__ = ["add_parser", "run"]

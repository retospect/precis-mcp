"""Patent watch CLI subcommands.

Three jobs share this module because they all operate on the saved-
watch row family introduced in phase 2 of the patent kind:

- ``watch-patents``      — create or delete a saved CQL watch.
- ``list-patent-watches`` — list every saved watch.
- ``run-patent-watches``  — run a one-shot pass over all due watches
                            (or one named watch).

The runner is hidden from the agent surface entirely — these CLI
subcommands are how operators interact with saved watches. We
surface them regardless of whether the ``EPO_OPS_*`` env vars are
set; the runtime check lives inside the runner itself.
"""

from __future__ import annotations

import argparse
import os
import sys

from precis.cli._common import resolve_dsn

# ---------------------------------------------------------------------------
# Interval parsing
# ---------------------------------------------------------------------------


def _parse_interval(spec: str) -> int:
    """Parse ``--every`` into seconds.

    Accepts ``Nh`` (hours), ``Nd`` (days), ``Nw`` (weeks), or a bare
    number of seconds. The CLI helper rejects anything else with a
    short error message — saved-watch intervals run for years, so a
    typo at create time would silently bake in a wrong cadence.

    Kept prefixed for backwards compatibility: the original CLI
    test imports it as ``from precis.cli import _parse_interval`` and
    the ``__init__`` re-exports it. Renaming to a public name would
    break pinned tests for no meaningful gain.
    """
    s = spec.strip().lower()
    if not s:
        raise ValueError("empty interval")
    if s.isdigit():
        return int(s)
    unit = s[-1]
    head = s[:-1]
    if not head.isdigit():
        raise ValueError(f"invalid interval {spec!r} - expected like '7d', '1h', '1w'")
    n = int(head)
    if n <= 0:
        raise ValueError(f"invalid interval {spec!r} - must be positive")
    multipliers = {"h": 3600, "d": 86_400, "w": 604_800}
    if unit not in multipliers:
        raise ValueError(f"invalid interval unit {unit!r} - use 'h', 'd', or 'w'")
    return n * multipliers[unit]


# ---------------------------------------------------------------------------
# Parser registration
# ---------------------------------------------------------------------------


def add_parsers(sub: argparse._SubParsersAction) -> None:
    """Register watch-patents, list-patent-watches, run-patent-watches."""
    wp = sub.add_parser(
        "watch-patents",
        help="Create a saved CQL patent watch (or delete one with --delete).",
    )
    wp.add_argument(
        "cql",
        nargs="?",
        default=None,
        help=(
            "CQL string (strict, no bare keywords). Required unless --delete is given."
        ),
    )
    wp.add_argument(
        "--name",
        required=True,
        help="Watch slug; used by run-patent-watches --name and --delete.",
    )
    wp.add_argument(
        "--every",
        default="7d",
        help=(
            "How often the watch should re-run. Accepts 'Nh' (hours), "
            "'Nd' (days), 'Nw' (weeks). Default: 7d."
        ),
    )
    wp.add_argument(
        "--max-per-pass",
        type=int,
        default=None,
        help=(
            "Cap how many patents this watch ingests / surfaces "
            "in a single pass. Overflow drops and resurfaces "
            "next pass. Default: no cap."
        ),
    )
    wp.add_argument(
        "--delete",
        action="store_true",
        help="Delete the watch with --name (cql positional ignored).",
    )
    wp.add_argument("--database-url", default=None)

    lp = sub.add_parser(
        "list-patent-watches",
        help="List every saved patent watch.",
    )
    lp.add_argument(
        "--show-cql",
        action="store_true",
        help="Include each watch's CQL in the output (long lines).",
    )
    lp.add_argument("--database-url", default=None)

    rp = sub.add_parser(
        "run-patent-watches",
        help="Run a one-shot pass over all due patent watches.",
    )
    rp.add_argument(
        "--name",
        default=None,
        help=(
            "Run exactly one watch by name, regardless of due-ness. "
            "Useful for debugging."
        ),
    )
    rp.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would happen; don't ingest or update last_run_at.",
    )
    rp.add_argument(
        "--fair-use-limit-gb",
        type=float,
        default=None,
        help=(
            "Override the rolling 7-day fair-use cap (default 3 GiB, "
            "or PRECIS_PATENT_FAIR_USE_LIMIT_GB env var)."
        ),
    )
    rp.add_argument("--database-url", default=None)

    # ── sweep-patent-fulltext ───────────────────────────────────────────
    sp = sub.add_parser(
        "sweep-patent-fulltext",
        help=(
            "Retry OPS description / claims endpoints for patents "
            "whose full text wasn't available at ingest time."
        ),
    )
    sp.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Max number of patents this pass will attempt. "
            "Overflow resurfaces on the next pass. Default: 50."
        ),
    )
    sp.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be retried; fetch nothing, mutate nothing.",
    )
    sp.add_argument(
        "--fair-use-limit-gb",
        type=float,
        default=None,
        help=(
            "Override the rolling 7-day fair-use cap (default 3 GiB, "
            "or PRECIS_PATENT_FAIR_USE_LIMIT_GB env var)."
        ),
    )
    sp.add_argument("--database-url", default=None)

    # ── fetch-google-patents ────────────────────────────────────────────
    gp = sub.add_parser(
        "fetch-google-patents",
        help=(
            "Fall-back full-text fetcher via patents.google.com. Picks "
            "patents tagged awaiting-fulltext or fulltext-unavailable "
            "(no gp-attempted tag), fetches the patents.google.com "
            "page, parses description + claims, and inserts blocks."
        ),
    )
    gp.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Max patents this pass will attempt. Overflow resurfaces "
            "on the next pass. Default: 10."
        ),
    )
    gp.add_argument(
        "--force",
        action="store_true",
        help=(
            "Re-attempt patents already tagged gp-attempted (cleared "
            "before the new fetch). Use when the parser has been "
            "updated, not in the steady-state cycle."
        ),
    )
    gp.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be fetched; fetch nothing, mutate nothing.",
    )
    gp.add_argument("--database-url", default=None)

    # ── reingest-patents ────────────────────────────────────────────────
    ri = sub.add_parser(
        "reingest-patents",
        help=(
            "Force-reingest already-ingested patents so their claim blocks "
            "carry the patent_block markers the freedom-to-operate digest "
            "reads. Re-fetches OPS XML, re-parses, and swaps each ref's "
            "blocks in place (id/links/tags preserved). Operator backfill."
        ),
    )
    ri.add_argument(
        "--slug",
        action="append",
        default=None,
        dest="slugs",
        help=(
            "Restrict to this DOCDB slug (repeatable). Default: every "
            "epo_ops patent, oldest-first."
        ),
    )
    ri.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max patents to re-ingest this pass (oldest-first). Default: all.",
    )
    ri.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be refetched; fetch nothing, mutate nothing.",
    )
    ri.add_argument(
        "--fair-use-limit-gb",
        type=float,
        default=None,
        help=(
            "Override the rolling 7-day fair-use cap (default 3 GiB, "
            "or PRECIS_PATENT_FAIR_USE_LIMIT_GB env var)."
        ),
    )
    ri.add_argument("--database-url", default=None)


# ---------------------------------------------------------------------------
# watch-patents
# ---------------------------------------------------------------------------


def run_watch(args: argparse.Namespace) -> None:
    """Implements ``precis jobs watch-patents`` — create or delete a watch."""
    from precis.config import load_config
    from precis.errors import PrecisError
    from precis.handlers import _patent_watch_db as watch_db
    from precis.store import Store

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    try:
        if args.delete:
            try:
                watch_db.delete(store, args.name)
            except PrecisError as e:
                print(f"watch-patents: {e}", file=sys.stderr)
                if e.next:
                    print(f"  next: {e.next}", file=sys.stderr)
                sys.exit(1)
            print(f"watch-patents: deleted {args.name!r}")
            return

        if args.cql is None:
            print(
                "watch-patents: cql is required (or pass --delete to remove a watch)",
                file=sys.stderr,
            )
            sys.exit(2)

        try:
            interval_s = _parse_interval(args.every)
        except ValueError as e:
            print(f"watch-patents: {e}", file=sys.stderr)
            sys.exit(2)

        try:
            watch = watch_db.create(
                store,
                name=args.name,
                cql=args.cql,
                interval_s=interval_s,
                max_per_pass=args.max_per_pass,
                created_by="cli",
            )
        except PrecisError as e:
            print(f"watch-patents: {e}", file=sys.stderr)
            if e.next:
                print(f"  next: {e.next}", file=sys.stderr)
            sys.exit(1)
        days = watch.interval_s / 86_400
        cap = (
            f"{watch.max_per_pass}/pass" if watch.max_per_pass is not None else "no cap"
        )
        print(
            f"watch-patents: created {watch.name!r} "
            f"[every {days:g}d, {cap}]\n"
            f"  cql: {watch.cql}"
        )
    finally:
        store.close()


# ---------------------------------------------------------------------------
# list-patent-watches
# ---------------------------------------------------------------------------


def run_list(args: argparse.Namespace) -> None:
    """Implements ``precis jobs list-patent-watches``."""
    from precis.config import load_config
    from precis.handlers import _patent_watch_db as watch_db
    from precis.store import Store

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    try:
        watches = watch_db.list_all(store)
        if not watches:
            print("list-patent-watches: no saved watches")
            return
        cols = f"{'NAME':<24}  {'EVERY':>6}  {'LAST RUN':<19}  {'SEEN':>5}"
        print(cols)
        print("-" * len(cols))
        for w in watches:
            days = w.interval_s / 86_400
            every_str = f"{days:g}d" if days >= 1 else f"{w.interval_s // 3600}h"
            last = (
                w.last_run_at.strftime("%Y-%m-%d %H:%M:%S")
                if w.last_run_at is not None
                else "(never)"
            )
            print(f"{w.name:<24}  {every_str:>6}  {last:<19}  {len(w.last_seen_pn):>5}")
            if args.show_cql:
                print(f"    cql: {w.cql}")
        print(f"-- total: {len(watches)}")
    finally:
        store.close()


# ---------------------------------------------------------------------------
# run-patent-watches
# ---------------------------------------------------------------------------


def run_runner(args: argparse.Namespace) -> None:
    """Implements ``precis jobs run-patent-watches``."""
    from pathlib import Path

    from precis.config import load_config
    from precis.embedder import make_embedder
    from precis.handlers._patent_ops import OpsClient
    from precis.jobs.patent_watch import (
        DEFAULT_FAIR_USE_LIMIT_GB,
        run_one_pass,
    )
    from precis.store import Store

    # Env vars must be set; without them OPS calls would fail
    # immediately with auth errors.
    epo_key = os.environ.get("EPO_OPS_CLIENT_KEY")
    epo_secret = os.environ.get("EPO_OPS_CLIENT_SECRET")
    raw_root_str = os.environ.get("PRECIS_PATENT_RAW_ROOT")
    if not (epo_key and epo_secret and raw_root_str):
        print(
            "run-patent-watches: EPO_OPS_CLIENT_KEY, "
            "EPO_OPS_CLIENT_SECRET, and PRECIS_PATENT_RAW_ROOT must all "
            "be set",
            file=sys.stderr,
        )
        sys.exit(2)

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    try:
        embedder = make_embedder(cfg.embedder, dim=store.embedding_dim())
        ops = OpsClient(
            key=epo_key,
            secret=epo_secret,
            user_agent=os.environ.get("EPO_OPS_USER_AGENT"),
        )

        fair_use_limit_gb = args.fair_use_limit_gb
        if fair_use_limit_gb is None:
            env_lim = os.environ.get("PRECIS_PATENT_FAIR_USE_LIMIT_GB")
            fair_use_limit_gb = float(env_lim) if env_lim else DEFAULT_FAIR_USE_LIMIT_GB

        summary = run_one_pass(
            store=store,
            ops=ops,
            embedder=embedder,
            raw_root=Path(raw_root_str).expanduser(),
            only_name=args.name,
            dry_run=args.dry_run,
            fair_use_limit_gb=fair_use_limit_gb,
        )

        if summary.paused_global:
            gb = summary.fair_use_bytes_before / (1024**3)
            print(
                f"run-patent-watches: paused - rolling 7d fair-use "
                f"{gb:.2f} GiB ≥ limit {fair_use_limit_gb:.2f} GiB"
            )
            return
        if not summary.results:
            print("run-patent-watches: no watches due")
            return
        for r in summary.results:
            if r.error is not None:
                print(f"  fail  {r.watch_name}  - {r.error}")
                continue
            if r.skipped_dry_run:
                print(
                    f"  dry   {r.watch_name}  "
                    f"({len(r.new_pn)} new patent{'s' if len(r.new_pn) != 1 else ''})"
                )
                continue
            ingest_part = f"ingested={len(r.ingested_pn)}" if r.ingested_pn else ""
            overflow_part = f"overflow={len(r.overflow_pn)}" if r.overflow_pn else ""
            details = (
                " ".join(p for p in (ingest_part, overflow_part) if p) or "no new hits"
            )
            print(f"  ok    {r.watch_name}  new={len(r.new_pn)}  {details}")
    finally:
        store.close()


# ---------------------------------------------------------------------------
# sweep-patent-fulltext
# ---------------------------------------------------------------------------


def run_fulltext_sweep_cli(args: argparse.Namespace) -> None:
    """Implements ``precis jobs sweep-patent-fulltext`` — retry the
    OPS description / claims endpoints for every awaiting-fulltext
    patent whose retry timestamp has matured.
    """
    from pathlib import Path

    from precis.config import load_config
    from precis.embedder import make_embedder
    from precis.handlers._patent_ops import OpsClient
    from precis.jobs.patent_fulltext_sweep import (
        DEFAULT_SWEEP_LIMIT,
        run_fulltext_sweep,
    )
    from precis.jobs.patent_watch import DEFAULT_FAIR_USE_LIMIT_GB
    from precis.store import Store

    # Env vars must be set; without them OPS calls would fail
    # immediately with auth errors.
    epo_key = os.environ.get("EPO_OPS_CLIENT_KEY")
    epo_secret = os.environ.get("EPO_OPS_CLIENT_SECRET")
    raw_root_str = os.environ.get("PRECIS_PATENT_RAW_ROOT")
    if not (epo_key and epo_secret and raw_root_str):
        print(
            "sweep-patent-fulltext: EPO_OPS_CLIENT_KEY, "
            "EPO_OPS_CLIENT_SECRET, and PRECIS_PATENT_RAW_ROOT must all "
            "be set",
            file=sys.stderr,
        )
        sys.exit(2)

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    try:
        embedder = make_embedder(cfg.embedder, dim=store.embedding_dim())
        ops = OpsClient(
            key=epo_key,
            secret=epo_secret,
            user_agent=os.environ.get("EPO_OPS_USER_AGENT"),
        )

        fair_use_limit_gb = args.fair_use_limit_gb
        if fair_use_limit_gb is None:
            env_lim = os.environ.get("PRECIS_PATENT_FAIR_USE_LIMIT_GB")
            fair_use_limit_gb = float(env_lim) if env_lim else DEFAULT_FAIR_USE_LIMIT_GB

        limit = args.limit if args.limit is not None else DEFAULT_SWEEP_LIMIT

        summary = run_fulltext_sweep(
            store=store,
            ops=ops,
            embedder=embedder,
            raw_root=Path(raw_root_str).expanduser(),
            limit=limit,
            dry_run=args.dry_run,
            fair_use_limit_gb=fair_use_limit_gb,
        )

        if summary.paused_global:
            gb = summary.fair_use_bytes_before / (1024**3)
            print(
                f"sweep-patent-fulltext: paused - rolling 7d fair-use "
                f"{gb:.2f} GiB ≥ limit {fair_use_limit_gb:.2f} GiB"
            )
            return
        if not summary.outcomes:
            print("sweep-patent-fulltext: no patents due for retry")
            return
        for o in summary.outcomes:
            if o.error is not None:
                print(f"  fail  {o.slug}  - {o.error}")
                continue
            if o.skipped_dry_run:
                status = "give up" if o.given_up else "retry"
                print(f"  dry   {o.slug}  (would {status})")
                continue
            if o.given_up:
                print(f"  gave  {o.slug}  - six-month window exceeded")
                continue
            if o.succeeded:
                print(f"  ok    {o.slug}  - +{o.blocks_added} blocks")
                continue
            print(f"  wait  {o.slug}  - still 404; retry rescheduled")
    finally:
        store.close()


# ---------------------------------------------------------------------------
# fetch-google-patents
# ---------------------------------------------------------------------------


def run_gp_fetch_cli(args: argparse.Namespace) -> None:
    """Implements ``precis jobs fetch-google-patents`` — backfill patent
    full text via patents.google.com for patents OPS couldn't serve.

    Unlike the OPS sweep this runner doesn't require any external
    credentials — patents.google.com serves the HTML page without
    auth. It still respects the ``PRECIS_GP_FETCH`` env gate so an
    ad-hoc run on a host that's been excluded from the steady-state
    pass is an explicit opt-in.
    """
    from precis.config import load_config
    from precis.store import Store
    from precis.workers.fetch_google_patents import (
        DEFAULT_GP_LIMIT,
        _is_enabled,
        run_gp_fetch_pass,
    )

    if not _is_enabled():
        print(
            "fetch-google-patents: PRECIS_GP_FETCH=1 must be set to opt in",
            file=sys.stderr,
        )
        sys.exit(2)

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    try:
        limit = args.limit if args.limit is not None else DEFAULT_GP_LIMIT
        result = run_gp_fetch_pass(
            store, limit=limit, force=args.force, dry_run=args.dry_run
        )
        if result["claimed"] == 0:
            print("fetch-google-patents: no patents due for retry")
            return
        print(
            f"fetch-google-patents: claimed={result['claimed']} "
            f"ok={result['ok']} failed={result['failed']}"
            + (" (dry-run)" if args.dry_run else "")
        )
    finally:
        store.close()


# ---------------------------------------------------------------------------
# reingest-patents
# ---------------------------------------------------------------------------


def run_reingest_cli(args: argparse.Namespace) -> None:
    """Implements ``precis jobs reingest-patents`` — force-reingest
    existing patents so their claim blocks carry the slice-1
    ``patent_block`` markers the freedom-to-operate digest reads
    (docs/design/patent-authoring-loop.md).
    """
    from pathlib import Path

    from precis.config import load_config
    from precis.embedder import make_embedder
    from precis.handlers._patent_ops import OpsClient
    from precis.jobs.patent_reingest import run_reingest_pass
    from precis.jobs.patent_watch import DEFAULT_FAIR_USE_LIMIT_GB
    from precis.store import Store

    epo_key = os.environ.get("EPO_OPS_CLIENT_KEY")
    epo_secret = os.environ.get("EPO_OPS_CLIENT_SECRET")
    raw_root_str = os.environ.get("PRECIS_PATENT_RAW_ROOT")
    if not (epo_key and epo_secret and raw_root_str):
        print(
            "reingest-patents: EPO_OPS_CLIENT_KEY, "
            "EPO_OPS_CLIENT_SECRET, and PRECIS_PATENT_RAW_ROOT must all "
            "be set",
            file=sys.stderr,
        )
        sys.exit(2)

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    try:
        embedder = make_embedder(cfg.embedder, dim=store.embedding_dim())
        ops = OpsClient(
            key=epo_key,
            secret=epo_secret,
            user_agent=os.environ.get("EPO_OPS_USER_AGENT"),
        )

        fair_use_limit_gb = args.fair_use_limit_gb
        if fair_use_limit_gb is None:
            env_lim = os.environ.get("PRECIS_PATENT_FAIR_USE_LIMIT_GB")
            fair_use_limit_gb = float(env_lim) if env_lim else DEFAULT_FAIR_USE_LIMIT_GB

        summary = run_reingest_pass(
            store=store,
            ops=ops,
            embedder=embedder,
            raw_root=Path(raw_root_str).expanduser(),
            only_slugs=args.slugs,
            limit=args.limit,
            dry_run=args.dry_run,
            fair_use_limit_gb=fair_use_limit_gb,
        )

        if summary.paused_global:
            gb = summary.fair_use_bytes_before / (1024**3)
            print(
                f"reingest-patents: paused - rolling 7d fair-use "
                f"{gb:.2f} GiB ≥ limit {fair_use_limit_gb:.2f} GiB"
            )
            return
        if not summary.outcomes:
            print("reingest-patents: no patents to re-ingest")
            return

        ok = failed = 0
        for o in summary.outcomes:
            if o.error is not None:
                failed += 1
                print(f"  fail  {o.slug}  - {o.error}")
                continue
            if o.skipped_dry_run:
                print(f"  dry   {o.slug}  ({o.blocks_before} blocks now)")
                continue
            ok += 1
            print(
                f"  ok    {o.slug}  blocks {o.blocks_before}→{o.blocks_after}"
                f"  (+{o.bytes_fetched} B)"
            )
        if args.dry_run:
            print(f"-- dry-run: {len(summary.outcomes)} patents would re-ingest")
        else:
            print(f"-- re-ingested ok={ok} failed={failed}")
    finally:
        store.close()


__all__ = [
    "_parse_interval",
    "add_parsers",
    "run_fulltext_sweep_cli",
    "run_gp_fetch_cli",
    "run_list",
    "run_reingest_cli",
    "run_runner",
    "run_watch",
]

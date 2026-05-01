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
        raise ValueError(f"invalid interval {spec!r} — expected like '7d', '1h', '1w'")
    n = int(head)
    if n <= 0:
        raise ValueError(f"invalid interval {spec!r} — must be positive")
    multipliers = {"h": 3600, "d": 86_400, "w": 604_800}
    if unit not in multipliers:
        raise ValueError(f"invalid interval unit {unit!r} — use 'h', 'd', or 'w'")
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
        "--auto-get",
        action="store_true",
        help=(
            "Ingest hits directly into the patent kind. "
            "Default is to open a quest summarising new hits."
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
        help="Report what would happen; don't write quests, ingest, or update last_run_at.",
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
                auto_get=args.auto_get,
                max_per_pass=args.max_per_pass,
                created_by="cli",
            )
        except PrecisError as e:
            print(f"watch-patents: {e}", file=sys.stderr)
            if e.next:
                print(f"  next: {e.next}", file=sys.stderr)
            sys.exit(1)
        mode = "auto-get" if watch.auto_get else "quest-on-new-hits"
        days = watch.interval_s / 86_400
        cap = (
            f"{watch.max_per_pass}/pass" if watch.max_per_pass is not None else "no cap"
        )
        print(
            f"watch-patents: created {watch.name!r} "
            f"[{mode}, every {days:g}d, {cap}]\n"
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
        cols = f"{'NAME':<24}  {'EVERY':>6}  {'MODE':<8}  {'LAST RUN':<19}  {'SEEN':>5}"
        print(cols)
        print("-" * len(cols))
        for w in watches:
            mode = "auto" if w.auto_get else "quest"
            days = w.interval_s / 86_400
            every_str = f"{days:g}d" if days >= 1 else f"{w.interval_s // 3600}h"
            last = (
                w.last_run_at.strftime("%Y-%m-%d %H:%M:%S")
                if w.last_run_at is not None
                else "(never)"
            )
            print(
                f"{w.name:<24}  {every_str:>6}  {mode:<8}  "
                f"{last:<19}  {len(w.last_seen_pn):>5}"
            )
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
                f"run-patent-watches: paused — rolling 7d fair-use "
                f"{gb:.2f} GiB ≥ limit {fair_use_limit_gb:.2f} GiB"
            )
            return
        if not summary.results:
            print("run-patent-watches: no watches due")
            return
        for r in summary.results:
            if r.error is not None:
                print(f"  fail  {r.watch_name}  — {r.error}")
                continue
            if r.skipped_dry_run:
                print(
                    f"  dry   {r.watch_name}  "
                    f"({len(r.new_pn)} new patent{'s' if len(r.new_pn) != 1 else ''})"
                )
                continue
            quest_part = f"quest={r.quest_slug}" if r.quest_slug is not None else ""
            ingest_part = f"ingested={len(r.ingested_pn)}" if r.ingested_pn else ""
            overflow_part = f"overflow={len(r.overflow_pn)}" if r.overflow_pn else ""
            details = (
                " ".join(p for p in (quest_part, ingest_part, overflow_part) if p)
                or "no new hits"
            )
            print(f"  ok    {r.watch_name}  new={len(r.new_pn)}  {details}")
    finally:
        store.close()


__all__ = [
    "_parse_interval",
    "add_parsers",
    "run_list",
    "run_runner",
    "run_watch",
]

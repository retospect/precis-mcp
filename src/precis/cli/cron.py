"""``precis cron tick`` — fire due scheduled ticks.

The standalone ``precis-cron-tick`` launchd timer that used to fire this
every 60s is retired (§15i/§A) — the decentralized ``scheduler`` worker pass
(live by default, both profiles — ``workers/scheduler.py`` CADENCES) now
owns the ``cron_tick`` cadence fleet-wide. This subcommand stays for a
manual/ad-hoc tick, delegating to
:func:`precis.workers.schedule.worker.run_schedule_pass`, the single
implementation shared with the scheduler pass's ``_run_cron_tick`` and the
default worker rotation's ``schedule`` pass — one engine, several triggers,
no drift. Historically this drove the now-retired ``kind='cron'`` engine
directly; ADR 0061 (superseding ADR 0030) folded that push-notification
mechanism onto recurring todos (``meta.schedule`` set — ``meta.deliver`` for
the push target, one-shot ``meta.schedule.at`` for "remind me in/at").

Exit code 0 always (failures log but don't break a caller; a next tick
recovers).
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

from precis.cli._common import resolve_dsn

log = logging.getLogger(__name__)


def add_parser(subparsers: Any) -> None:
    """Register the ``cron`` subparser tree on the top-level parser."""
    cron = subparsers.add_parser(
        "cron",
        help="Cron operations — tick the scheduled-task scanner.",
    )
    cron_sub = cron.add_subparsers(dest="cron_cmd", required=True)
    cron_sub.add_parser(
        "tick",
        help=(
            "Fire due recurring ticks (spawn + push delivery). "
            "Launchd timer runs this every 60s on melchior."
        ),
    )


def run(args: argparse.Namespace) -> None:
    """Dispatch ``precis cron <subcmd>``."""
    if args.cron_cmd == "tick":
        _tick()
        return
    import sys

    print(f"cron: unknown subcommand {args.cron_cmd!r}", file=sys.stderr)
    sys.exit(2)


def _tick() -> None:
    """Run one ``schedule`` pass — the actual engine lives there now."""
    from precis.store import Store
    from precis.workers.schedule.worker import run_schedule_pass

    dsn = resolve_dsn(None)
    store = Store.connect(dsn, min_size=1, max_size=2)
    try:
        result = run_schedule_pass(store, limit=200)
    finally:
        store.close()
    log.info(
        "cron tick: claimed=%d, ok=%d, failed=%d",
        result.claimed,
        result.ok,
        result.failed,
    )

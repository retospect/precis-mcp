"""``precis heartbeat`` — report this host's liveness + sensors.

A one-shot reporter each machine runs on a timer (launchd / systemd-timer /
cron), delegating its collection+upsert core to
:mod:`precis.workers.heartbeat` (§A) — the same module a ``heartbeat``
worker pass now also calls once per system-worker cycle, self-throttled, so
manual/cron invocations and the pass share one implementation. See that
module's docstring for the collected fields (load, best-effort CPU temp) and
the temperature-probe priority order.
"""

from __future__ import annotations

import argparse

from precis.cli._common import resolve_dsn


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``heartbeat`` subcommand."""
    p = sub.add_parser(
        "heartbeat",
        help="Report this host's load + CPU temp to host_heartbeat.",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )
    p.add_argument(
        "--host",
        default=None,
        help="Override the reported host name (default PRECIS_HOST_NAME / hostname).",
    )
    p.set_defaults(func=run)
    return p


def run(args: argparse.Namespace) -> None:
    """Collect this host's snapshot and UPSERT it into ``host_heartbeat``."""
    from precis.store import Store
    from precis.workers.heartbeat import collect_and_report

    dsn = resolve_dsn(getattr(args, "database_url", None))
    store = Store.connect(dsn)
    try:
        line = collect_and_report(store, getattr(args, "host", None))
    finally:
        store.close()
    print(line)


__all__ = [
    "add_parser",
    "run",
]

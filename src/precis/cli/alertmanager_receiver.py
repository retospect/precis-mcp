"""``precis alertmanager-receiver`` — Alertmanager webhook -> ops channel.

Run beside Alertmanager (the monitoring role's systemd unit); see
:mod:`precis.workers.alertmanager_receiver` for the contract.
"""

from __future__ import annotations

import argparse
import sys

from precis.cli._common import resolve_dsn

_LOOPBACK = ("127.0.0.1", "localhost")


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p = sub.add_parser(
        "alertmanager-receiver",
        help="Receive Alertmanager webhooks and queue them to the ops channel.",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")
    p.add_argument("--host", default="127.0.0.1", help="Bind address (loopback only).")
    p.add_argument("--port", type=int, default=9095, help="Bind port.")
    p.set_defaults(func=run)
    return p


def run(args: argparse.Namespace) -> None:
    if args.host not in _LOOPBACK:
        # The endpoint has no auth of its own; loopback is the boundary.
        print(
            f"refusing to bind {args.host!r}: the receiver is unauthenticated "
            "and must stay on loopback",
            file=sys.stderr,
        )
        sys.exit(2)
    from precis.store import Store
    from precis.workers.alertmanager_receiver import serve

    store = Store.connect(resolve_dsn(getattr(args, "database_url", None)))
    try:
        serve(store, args.host, args.port)
    finally:
        store.close()


__all__ = ["add_parser", "run"]

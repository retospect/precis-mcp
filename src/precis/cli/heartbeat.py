"""``precis heartbeat`` — report this host's liveness + sensors.

A one-shot reporter each machine runs on a timer (launchd / systemd-timer /
cron), delegating its collection+upsert core to
:mod:`precis.workers.heartbeat` (§A) — the same module a ``heartbeat``
worker pass now also calls once per system-worker cycle, self-throttled, so
manual/cron invocations and the pass share one implementation. See that
module's docstring for the collected fields (load, best-effort CPU temp) and
the temperature-probe priority order.

``--retire <host>`` / ``--unretire <host>`` stamp and clear
``host_heartbeat.meta.retired``, which is how a decommissioned host is taken
off nursery's ``host-dark`` detector. It is the only supported way: the
detector used to let a dark host age out of a 30-day ``worker_logs`` lookback,
and since the sweeper prunes that table on the same horizon, a host that broke
and stayed broke disappeared for exactly the same reason a retired one did
(``docs/backlog/host-dark-ages-out-with-worker-logs-retention.md``). Retiring
is a decision someone records, not a side effect of log retention.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from precis.cli._common import resolve_dsn

if TYPE_CHECKING:
    from precis.store import Store


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
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--retire",
        metavar="HOST",
        default=None,
        help="Mark HOST decommissioned (meta.retired) so host-dark stops "
        "paging for its lingering row. Reports nothing else.",
    )
    g.add_argument(
        "--unretire",
        metavar="HOST",
        default=None,
        help="Clear HOST's retired marker, putting it back under host-dark.",
    )
    p.set_defaults(func=run)
    return p


#: A host whose heartbeat is fresher than this is still beating, so
#: ``--retire`` refuses it. Not a safety rail for its own sake: the
#: ``record_heartbeat`` UPSERT replaces ``meta`` wholesale apart from
#: ``boot_ids``/``activity``, so a live host's next beat clears the marker
#: within a minute and the operator would be left believing it stuck. The
#: same mechanism is a feature once the host is genuinely down — a retired
#: host that comes back un-retires itself and resumes being monitored.
RETIRE_MAX_FRESH_MIN = 15


def _retire(store: Store, host: str, *, retired: bool) -> str:
    """Stamp/clear the marker, refusing to retire a host that is still live."""
    from datetime import UTC, datetime

    beats = {hb.host: hb for hb in store.recent_heartbeats()}
    hb = beats.get(host)
    if hb is None:
        known = ", ".join(sorted(beats)) or "(none)"
        raise SystemExit(
            f"no host_heartbeat row for {host!r} — nothing to "
            f"{'retire' if retired else 'unretire'}. Known hosts: {known}"
        )
    if retired:
        age_min = (datetime.now(UTC) - hb.ts).total_seconds() / 60
        if age_min < RETIRE_MAX_FRESH_MIN:
            raise SystemExit(
                f"{host} last beat {age_min:.1f} min ago — it is still running. "
                "Stop its worker first: a live host's next beat replaces meta "
                "and would clear the marker you just set."
            )
    changed = store.set_host_retired(host, retired=retired)
    verb = "retired" if retired else "un-retired"
    return f"{host}: {verb}" if changed else f"{host}: no change (already {verb})"


def run(args: argparse.Namespace) -> None:
    """Collect this host's snapshot and UPSERT it into ``host_heartbeat``."""
    from precis.store import Store
    from precis.workers.heartbeat import collect_and_report

    dsn = resolve_dsn(getattr(args, "database_url", None))
    store = Store.connect(dsn)
    try:
        retire = getattr(args, "retire", None)
        unretire = getattr(args, "unretire", None)
        if retire or unretire:
            line = _retire(store, str(retire or unretire), retired=bool(retire))
        else:
            line = collect_and_report(store, getattr(args, "host", None))
    finally:
        store.close()
    print(line)


__all__ = [
    "RETIRE_MAX_FRESH_MIN",
    "add_parser",
    "run",
]

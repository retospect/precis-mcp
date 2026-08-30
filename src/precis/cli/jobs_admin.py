"""``precis jobs kill`` — operator force-kill backstop (§B-2 piece 5).

Only the OWNING executor (``ssh_node`` / ``claude_docker`` — the only two
that hold the ``ctx``/handle machinery needed to actually stop a remote
process or container) can drive a running job terminal; a CLI on some
other host cannot. So this verb only REQUESTS: it stamps
``meta.kill_requested`` on a ``STATUS:running`` ``kind='job'`` ref
(refusing anything else, with no write, for a clear operator error), and
the owning executor's poll loop honors it at its next tick — the SAME
kill->terminal-fail path it already uses for a wall-clock deadline kill
(``workers/executors/ssh_node.py``'s ``_poll_one`` /
``claude_docker.py``'s ``_poll_job``).

**Latency.** Takes effect at the next poll for a detached (submit/poll)
job_type — one worker pass. A legacy blocking ``dispatch`` job_type (no
``submit``/``poll``) can't be observed mid-run at all; the kill request
sits until that blocking call returns on its own.

``coordinator`` / ``claude_inproc`` are out of scope — in-process/cloud
calls have no owning poll loop to honor this, and no box compute to
reclaim.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import UTC, datetime
from typing import Any

from precis.cli._common import resolve_dsn
from precis.store import Store
from precis.workers.executors._common import RUNNING, current_status, set_meta


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``kill`` subparser on the ``jobs`` group."""
    kp = sub.add_parser(
        "kill",
        help="Request an operator force-kill of a running kind='job' ref.",
        description=(
            "Stamp meta.kill_requested on a STATUS:running job; the owning "
            "executor (ssh_node / claude_docker) honors it at its next poll "
            "tick, the same way it already handles a wall-clock deadline "
            "kill. Takes effect at the next poll for a detached job — a "
            "no-op for a legacy blocking dispatch until that call returns "
            "on its own."
        ),
    )
    kp.add_argument("ref_id", type=int, help="The job ref_id to kill.")
    kp.add_argument("--note", default=None, help="Free-text reason (forensic).")
    kp.add_argument("--database-url", default=None, help="Postgres DSN override.")
    return kp


def _cmd_kill(store: Store, args: argparse.Namespace) -> None:
    """Validate + stamp ``meta.kill_requested`` on ``args.ref_id``.

    Split out from :func:`run` (mirrors ``cli/service.py``'s ``_cmd_*``
    convention) so a test can drive it against a real ``store`` fixture
    without a DSN/connect round-trip.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT kind FROM refs WHERE ref_id = %s AND retired_at IS NULL",
            (args.ref_id,),
        ).fetchone()
        if row is None:
            print(f"jobs kill: no such ref {args.ref_id}", file=sys.stderr)
            sys.exit(2)
        if row[0] != "job":
            print(
                f"jobs kill: ref {args.ref_id} is kind={row[0]!r}, not 'job' "
                "— refusing",
                file=sys.stderr,
            )
            sys.exit(2)
        status = current_status(conn, args.ref_id)
        if status != RUNNING:
            print(
                f"jobs kill: ref {args.ref_id} is STATUS:{status or '<none>'}, "
                "not running — refusing (a kill only applies to an "
                "in-flight job)",
                file=sys.stderr,
            )
            sys.exit(2)
        request: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(),
            "actor": getpass.getuser(),
        }
        if args.note:
            request["note"] = args.note
        set_meta(conn, args.ref_id, kill_requested=request)
        conn.commit()
    print(
        f"jobs kill: requested kill of job {args.ref_id} — takes effect "
        "at the owning executor's next poll tick (immediate for a "
        "detached job; a legacy blocking dispatch can't be observed "
        "mid-run and only reacts once that call returns)"
    )


def run(args: argparse.Namespace) -> None:
    """Implements ``precis jobs kill <ref_id>``."""
    store = Store.connect(resolve_dsn(args.database_url))
    try:
        _cmd_kill(store, args)
    finally:
        store.close()


__all__ = ["add_parser", "run"]

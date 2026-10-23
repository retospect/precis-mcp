"""``precis limits`` — the outbound-policy override layer's write door.

``precis limits set <row>`` writes the ``external_rate_limits`` override
columns (migration 0233) — the DB layer of the outbound-policy merge
(docs/backlog/outbound-policy.md §3) and the only way to retune a
provider's admission policy without a deploy. The resolver's per-process
snapshot picks the change up within ~60 s (30 s TTL, worst case a
refresh cycle late); no worker restart.

``--concurrency`` is the spec's two readings in one flag: a resource
name writes the row's ``concurrency`` override column; a bare integer
edits the ``capacity`` of the resolved resource's ``resource_slots``
row — "how many at once" lives where the holds are counted (D2), never
in two places. ``--dry-run`` resolves the effective policy before and
after through :func:`precis.utils.outbound_policy.explain` and writes
nothing; a real run logs the actor and the field-by-field diff to
``worker_logs`` so who changed what is auditable.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import TYPE_CHECKING, Any

from precis.cli._common import resolve_dsn

if TYPE_CHECKING:
    from precis.store import Store

_HELP = "Outbound policy overrides (external_rate_limits): set [--dry-run]."

#: CLI flag → override column. ``class`` is handled separately (it is a
#: reserved word and needs quoting in SQL).
_FLAG_TO_COL = {
    "max_wait": "max_wait_s",
    "connect": "connect_s",
    "read": "read_s",
    "total": "total_s",
}

#: ``--clear`` name → override column.
_CLEAR_TO_COL = {
    "class": "class",
    "max-wait": "max_wait_s",
    "connect": "connect_s",
    "read": "read_s",
    "total": "total_s",
    "on-refuse": "on_refuse",
    "concurrency": "concurrency",
}

#: The row an absent ``limits set`` target gets — the same
#: ``(capacity, refill_per_sec)`` ``_resolve_provider`` seeds a
#: ``host:<netloc>`` row at. An ``llm-api`` row's tokens are unused; the
#: row only carries overrides and ``parked_until``.
_SEED_ROW = (1, 1.0)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``precis limits`` and its subcommands."""
    p = subparsers.add_parser(
        "limits",
        help=_HELP,
        description=(
            "Write the external_rate_limits override columns — the DB "
            "layer of the outbound-policy merge (class default -> "
            "POLICIES -> DB row). Agents read the same state through "
            "get(kind='job', id='/limits')."
        ),
    )
    ssub = p.add_subparsers(dest="limits_cmd", required=True)

    s = ssub.add_parser(
        "set",
        help="Write a row's override columns (audited; --dry-run previews).",
        description=(
            "Set override columns on one external_rate_limits row. Every "
            "non-NULL column overrides the code layers for that row; "
            "--clear returns a column to NULL (inherit). The change "
            "reaches workers within ~60s via the resolver snapshot."
        ),
    )
    s.add_argument(
        "provider",
        help="The limiter row — a named provider ('crossref') or "
        "'host:<netloc>' ('host:openrouter.ai'); the /limits view "
        "names them.",
    )
    s.add_argument(
        "--class",
        "--cls",
        dest="cls",
        default=None,
        choices=[
            "scraped-host",
            "provider-api",
            "llm-api",
            "llm-local",
            "internal",
        ],
        help="Policy class override.",
    )
    s.add_argument(
        "--max-wait",
        type=float,
        default=None,
        help="max_wait_s override — bound on a limiter/slot wait (>=0; "
        "0 = never wait).",
    )
    s.add_argument(
        "--connect", type=float, default=None, help="connect_s timeout override."
    )
    s.add_argument("--read", type=float, default=None, help="read_s override.")
    s.add_argument("--total", type=float, default=None, help="total_s override.")
    s.add_argument(
        "--on-refuse",
        default=None,
        choices=["wait", "defer", "fail"],
        help="on_refuse override — 'wait' proceeds unpaced after the "
        "budget, 'defer' raises RateLimited (requeue), 'fail' refuses "
        "immediately.",
    )
    s.add_argument(
        "--concurrency",
        default=None,
        metavar="RESOURCE|N",
        help="A resource name writes the 'concurrency' override column; "
        "a bare integer N edits the resolved resource's "
        "resource_slots(api/...) capacity — the hold row's own count.",
    )
    s.add_argument(
        "--clear",
        action="append",
        default=[],
        choices=sorted(_CLEAR_TO_COL),
        metavar="FIELD",
        help="NULL an override column back to inherit "
        "(repeatable). Fields: " + ", ".join(sorted(_CLEAR_TO_COL)),
    )
    s.add_argument(
        "--create",
        action="store_true",
        help="Insert a missing row at the (1, 1.0) default first — "
        "host:<netloc> rows always auto-seed; named rows refuse "
        "without this.",
    )
    s.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve the effective policy before and after; write nothing.",
    )
    s.add_argument("--actor", default=None, help="Who made the change (audit).")
    s.add_argument("--database-url", default=None, help="Postgres DSN override.")


def _collect_changes(args: argparse.Namespace) -> tuple[dict[str, Any], int | None]:
    """The ``{column: value|None}`` write set plus an optional
    ``resource_slots`` capacity (``--concurrency N``)."""
    changes: dict[str, Any] = {}
    if args.cls is not None:
        changes["class"] = args.cls
    for flag, col in _FLAG_TO_COL.items():
        value = getattr(args, flag)
        if value is None:
            continue
        if flag == "max_wait" and value < 0:
            print("limits: --max-wait must be >= 0", file=sys.stderr)
            sys.exit(2)
        if flag != "max_wait" and value <= 0:
            print(f"limits: --{flag} must be > 0", file=sys.stderr)
            sys.exit(2)
        changes[col] = value
    if args.on_refuse is not None:
        changes["on_refuse"] = args.on_refuse
    slot_capacity: int | None = None
    if args.concurrency is not None:
        try:
            slot_capacity = int(args.concurrency)
        except ValueError:
            changes["concurrency"] = args.concurrency
        else:
            if slot_capacity < 1:
                print("limits: --concurrency N must be >= 1", file=sys.stderr)
                sys.exit(2)
    for field in args.clear or []:
        changes[_CLEAR_TO_COL[field]] = None
    if not changes and slot_capacity is None:
        print(
            "limits: nothing to change — pass an override, --clear, or --concurrency N",
            file=sys.stderr,
        )
        sys.exit(2)
    return changes, slot_capacity


def _row_to_overrides(r: Any) -> dict[str, Any]:
    """The ``explain`` overrides map from the SELECTed row — same keys
    the resolver's snapshot carries."""
    return {
        "class": r[4],
        "max_wait_s": r[5],
        "connect_s": r[6],
        "read_s": r[7],
        "total_s": r[8],
        "on_refuse": r[9],
        "concurrency": r[10],
        "parked_until": r[11],
        "capacity": r[1],
        "refill_per_sec": r[2],
        "daily_cap": r[3],
    }


_SELECT_ROW = (
    "SELECT provider, capacity, refill_per_sec, daily_cap, "
    '"class", max_wait_s, connect_s, read_s, total_s, on_refuse, '
    "concurrency, parked_until FROM external_rate_limits "
    "WHERE provider = %s FOR UPDATE"
)


def _fmt(value: Any) -> str:
    return "-" if value is None else str(value)


def _effective_lines(
    provider: str, overrides: dict[str, Any], carriers: set[str]
) -> list[str]:
    """The ``field=value (layer)`` lines for every interpretation of the
    row — the same render the ``/limits`` view shows."""
    from precis.utils import outbound_policy

    lines = []
    for label, pol, url in outbound_policy.row_resolutions(
        provider, llm_api_carriers=carriers
    ):
        eff = outbound_policy.explain(label, pol, provider, url, overrides)
        via = "" if label == provider else f" (via {label})"
        lines.append(f"  effective{via}: {outbound_policy.format_effective(eff)}")
    return lines


def _cmd_set(store: Store, args: argparse.Namespace) -> None:
    provider = args.provider.strip()
    if not provider:
        print("limits: empty provider row name", file=sys.stderr)
        sys.exit(2)
    changes, slot_capacity = _collect_changes(args)

    from precis.utils import outbound_policy, rate_limit

    with store.pool.connection() as conn:
        with conn.transaction():
            if not rate_limit.s1_columns_present(conn):
                print(
                    "limits: override columns missing — migration 0233 "
                    "is not applied on this database",
                    file=sys.stderr,
                )
                sys.exit(2)
            row = conn.execute(_SELECT_ROW, (provider,)).fetchone()
            seeded_note = None
            if row is None:
                if provider.startswith("host:"):
                    seeded_note = "auto-seeded at (1, 1.0)"
                elif args.create:
                    seeded_note = "inserted at (1, 1.0) by --create"
                else:
                    print(
                        f"limits: no external_rate_limits row {provider!r} — "
                        "pass --create to seed one (host:<netloc> rows "
                        "auto-seed). Rows are listed by /limits.",
                        file=sys.stderr,
                    )
                    sys.exit(2)
                if not args.dry_run:
                    rate_limit._seed_host_row(conn, provider, _SEED_ROW)
                    row = conn.execute(_SELECT_ROW, (provider,)).fetchone()
                    if row is None:
                        print(
                            f"limits: could not seed {provider!r} — the "
                            "host-row cap may be full of live rows",
                            file=sys.stderr,
                        )
                        sys.exit(2)
            before = (
                _row_to_overrides(row)
                if row is not None
                else {
                    "class": None,
                    "max_wait_s": None,
                    "connect_s": None,
                    "read_s": None,
                    "total_s": None,
                    "on_refuse": None,
                    "concurrency": None,
                    "parked_until": None,
                    "capacity": _SEED_ROW[0],
                    "refill_per_sec": _SEED_ROW[1],
                    "daily_cap": None,
                }
            )
            after = {**before, **changes}

            carriers = {
                str(r[0])
                for r in conn.execute(
                    "SELECT resource FROM resource_slots "
                    "WHERE host = 'api' AND resource LIKE 'llm-api:%'"
                ).fetchall()
            }
            resolutions = outbound_policy.row_resolutions(
                provider, llm_api_carriers=carriers
            )
            slot_resource: str | None = None
            slot_before: int | None = None
            if slot_capacity is not None:
                # The capacity edit lands on the resource the resolved
                # policy actually names — the first interpretation that
                # has one (an llm-api carrier resolves it under
                # openai-tools, not the provider-less default).
                for label, pol, url in resolutions:
                    eff = outbound_policy.explain(label, pol, provider, url, after)
                    if eff.concurrency is not None:
                        slot_resource = eff.concurrency
                        break
                if slot_resource is None:
                    print(
                        f"limits: {provider!r} resolves no concurrency "
                        "resource — name one with --concurrency <resource>",
                        file=sys.stderr,
                    )
                    sys.exit(2)
                existing = conn.execute(
                    "SELECT capacity FROM resource_slots "
                    "WHERE host = 'api' AND resource = %s",
                    (slot_resource,),
                ).fetchone()
                slot_before = int(existing[0]) if existing else None

            if not args.dry_run:
                if changes:
                    sets = ", ".join(f'"{c}" = %s' for c in changes)
                    conn.execute(
                        f"UPDATE external_rate_limits SET {sets} WHERE provider = %s",
                        (*changes.values(), provider),
                    )
                if slot_resource is not None:
                    store.sync_host_resource_slots(
                        "api", {slot_resource: slot_capacity}, conn=conn
                    )
                _audit(
                    conn,
                    provider,
                    before,
                    changes,
                    slot_resource,
                    slot_before,
                    slot_capacity,
                    args.actor,
                    seeded_note,
                )

    lines = []
    if args.dry_run:
        lines.append(f"dry-run — nothing written for {provider!r}")
    else:
        diff = ", ".join(
            f"{c} {_fmt(before.get(c))} -> {_fmt(after.get(c))}" for c in changes
        )
        lines.append(f"external_rate_limits[{provider}]: {diff or 'unchanged'}")
    if seeded_note:
        lines.append(f"  row {seeded_note}")
    if slot_resource is not None:
        verb = "would set" if args.dry_run else "set"
        lines.append(
            f"  {verb} resource_slots[api/{slot_resource}] capacity "
            f"{_fmt(slot_before)} -> {slot_capacity}"
        )
    if args.dry_run:
        lines.append("  before:")
        lines.extend(_effective_lines(provider, before, carriers))
        lines.append("  after:")
        lines.extend(_effective_lines(provider, after, carriers))
    else:
        lines.append("  effective:")
        lines.extend(_effective_lines(provider, after, carriers))
    print("\n".join(lines))


def _actor(args_actor: str | None) -> str:
    if args_actor:
        return args_actor
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _audit(
    conn: Any,
    provider: str,
    before: dict[str, Any],
    changes: dict[str, Any],
    slot_resource: str | None,
    slot_before: int | None,
    slot_capacity: int | None,
    actor: str | None,
    seeded_note: str | None,
) -> None:
    """The 'who changed what' row: one worker_logs entry per real write,
    carrying the actor and the field-by-field diff as jsonb payload."""
    from precis.utils.db_log_handler import (
        _resolve_host_name,
        _resolve_process_name,
    )

    payload: dict[str, Any] = {
        "event": "limits-set",
        "provider": provider,
        "actor": _actor(actor),
        "changes": {
            c: {"old": _jsonable(before.get(c)), "new": _jsonable(n)}
            for c, n in changes.items()
        },
    }
    if slot_resource is not None:
        payload["slots"] = {
            "resource": slot_resource,
            "old": slot_before,
            "new": slot_capacity,
        }
    if seeded_note:
        payload["seeded"] = seeded_note
    conn.execute(
        "INSERT INTO worker_logs "
        "(host, process, level, logger, message, payload) "
        "VALUES (%s, %s, 'INFO', 'precis.cli.limits', %s, %s::jsonb)",
        (
            _resolve_host_name(),
            _resolve_process_name(),
            f"limits set {provider}",
            json.dumps(payload, default=str),
        ),
    )


def run(args: argparse.Namespace) -> None:
    """Dispatch ``precis limits <cmd>``."""
    from precis.store import Store

    store = Store.connect(resolve_dsn(args.database_url))
    try:
        if args.limits_cmd == "set":
            _cmd_set(store, args)
        else:  # pragma: no cover — argparse `required=True` guards this
            raise SystemExit(f"unknown limits subcommand: {args.limits_cmd!r}")
    finally:
        store.close()


__all__ = ["add_parser", "run"]

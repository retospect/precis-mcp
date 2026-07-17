"""``precis service`` — live worker run control (factory slice 2).

Read/write the ``service_config`` table (migration 0072): which passes
run on which host, at what claim weight (``prio`` 0..10; 0 = off), on
which model. The worker consults it live, so a flip takes effect on the
next loop cycle — no plist edit, no redeploy. This CLI is the surface
that makes slice 2 provable before the ``/factory`` console (slice 3/4)
exists; the console writes the same rows.

Examples::

    precis service list
    precis service prio melchior classify 0      # turn classify off on melchior
    precis service prio '*' llm_reconcile 3      # on everywhere at weight 3
    precis service model melchior briefing claude-opus-4-8
    precis service clear melchior classify       # revert to env/profile default
"""

from __future__ import annotations

import argparse

from precis.cli._common import resolve_dsn
from precis.store import Store
from precis.workers.registry import SERVICES_BY_NAME
from precis.workers.service_config import (
    clear_service_config,
    list_service_config,
    set_service_model,
    set_service_prio,
)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``precis service`` and its subcommands."""
    p = subparsers.add_parser(
        "service",
        help="Live worker run control (service_config): prio / model / clear.",
        description=(
            "Read and write the service_config table that gates worker "
            "passes live (prio 0 = off, 1..10 = claim weight). The worker "
            "picks up a change on its next loop cycle — no redeploy."
        ),
    )
    ssub = p.add_subparsers(dest="service_cmd", required=True)

    ls = ssub.add_parser("list", help="List all configured rows.")
    ls.add_argument("--database-url", default=None, help="Postgres DSN override.")

    pr = ssub.add_parser("prio", help="Set a service's prio (0 = off, 1..10).")
    pr.add_argument("host", help="Host name, or '*' for all hosts.")
    pr.add_argument("service", help="Service/pass name (e.g. classify).")
    pr.add_argument("prio", type=int, help="0 = off; 1..10 = claim weight.")
    pr.add_argument("--actor", default=None, help="Who made the change (audit).")
    pr.add_argument("--database-url", default=None, help="Postgres DSN override.")

    md = ssub.add_parser("model", help="Pin (or clear) a service's model_pref.")
    md.add_argument("host", help="Host name, or '*' for all hosts.")
    md.add_argument("service", help="Service/pass name.")
    md.add_argument(
        "model",
        nargs="?",
        default=None,
        help="Model id / llm-card key. Omit (with --clear) to unpin.",
    )
    md.add_argument("--clear", action="store_true", help="Unpin the model.")
    md.add_argument("--actor", default=None, help="Who made the change (audit).")
    md.add_argument("--database-url", default=None, help="Postgres DSN override.")

    cl = ssub.add_parser("clear", help="Delete a row (revert to env/profile default).")
    cl.add_argument("host", help="Host name, or '*' for all hosts.")
    cl.add_argument("service", help="Service/pass name.")
    cl.add_argument("--database-url", default=None, help="Postgres DSN override.")


def _warn_unknown_service(name: str) -> None:
    """Note when a name isn't a registered pass — a likely typo, not fatal.

    A row can legitimately name something not (yet) in the registry, so
    this only warns; the write still goes through.
    """
    if name not in SERVICES_BY_NAME:
        known = ", ".join(sorted(SERVICES_BY_NAME))
        print(
            f"note: {name!r} is not a known service (typo?). Known: {known}",
        )


def _cmd_list(store: Store) -> None:
    rows = list_service_config(store)
    if not rows:
        print("service_config is empty — all passes at their env/profile default.")
        return
    print(f"{'host':<14} {'service':<20} {'prio':>4}  {'model_pref':<24} actor")
    for r in rows:
        print(
            f"{r['host']!s:<14} {r['service']!s:<20} "
            f"{r['prio']!s:>4}  {r['model_pref'] or '-'!s:<24} "
            f"{r['actor'] or '-'}"
        )


def _cmd_prio(store: Store, args: argparse.Namespace) -> None:
    _warn_unknown_service(args.service)
    set_service_prio(store, args.host, args.service, args.prio, actor=args.actor)
    state = "OFF" if args.prio == 0 else f"weight {args.prio}"
    print(f"service_config: {args.host}/{args.service} → prio {args.prio} ({state})")


def _cmd_model(store: Store, args: argparse.Namespace) -> None:
    _warn_unknown_service(args.service)
    model = None if args.clear else args.model
    set_service_model(store, args.host, args.service, model, actor=args.actor)
    shown = "(cleared)" if model is None else model
    print(f"service_config: {args.host}/{args.service} model_pref → {shown}")


def _cmd_clear(store: Store, args: argparse.Namespace) -> None:
    removed = clear_service_config(store, args.host, args.service)
    if removed:
        print(f"service_config: removed {args.host}/{args.service}")
    else:
        print(f"service_config: no row for {args.host}/{args.service}")


def run(args: argparse.Namespace) -> None:
    """Dispatch ``precis service <cmd>``."""
    store = Store.connect(resolve_dsn(args.database_url))
    try:
        if args.service_cmd == "list":
            _cmd_list(store)
        elif args.service_cmd == "prio":
            _cmd_prio(store, args)
        elif args.service_cmd == "model":
            _cmd_model(store, args)
        elif args.service_cmd == "clear":
            _cmd_clear(store, args)
        else:  # pragma: no cover — argparse `required=True` guards this
            raise SystemExit(f"unknown service subcommand: {args.service_cmd!r}")
    finally:
        store.close()


__all__ = ["add_parser", "run"]

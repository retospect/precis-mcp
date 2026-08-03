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
    precis service seed melchior classify 5      # deploy-time only: insert
                                                  # iff absent, never clobbers
                                                  # a console override
    precis service reserve --hours 2             # §B-2: stop new heavy
                                                  # claims (ssh_node/
                                                  # claude_docker) on THIS
                                                  # host for 2h
    precis service release                       # lift this host's reserve
"""

from __future__ import annotations

import argparse
import sys

from precis.cli._common import resolve_dsn
from precis.corpus_layout import host_name
from precis.store import Store
from precis.workers.registry import SERVICES_BY_NAME
from precis.workers.service_config import (
    ALL_HOSTS,
    RESERVE_SERVICE,
    clear_reserve,
    clear_service_config,
    list_service_config,
    seed_service_prio,
    set_reserve,
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

    sd = ssub.add_parser(
        "seed",
        help="Insert a row ONLY if absent (deploy-time use — never clobbers "
        "a console override).",
    )
    sd.add_argument("host", help="Host name, or '*' for all hosts.")
    sd.add_argument("service", help="Service/pass name (e.g. classify).")
    sd.add_argument("prio", type=int, help="0 = off; 1..10 = claim weight.")
    sd.add_argument("--actor", default=None, help="Who made the change (audit).")
    sd.add_argument("--database-url", default=None, help="Postgres DSN override.")

    rs = ssub.add_parser(
        "reserve",
        help="Stop new heavy (ssh_node/claude_docker) claims on a host "
        "(§B-2) — auto-expires.",
    )
    rs.add_argument(
        "--host",
        default=None,
        help="Host to reserve (default: this host, via the same host_name() "
        "identity the claim gate uses).",
    )
    rs.add_argument(
        "--all", action="store_true", help="Reserve every host ('*') instead."
    )
    rs.add_argument(
        "--hours",
        type=float,
        default=4.0,
        help="Reserve duration in hours (default 4; refuses <= 0 or > 168).",
    )
    rs.add_argument("--actor", default=None, help="Who made the change (audit).")
    rs.add_argument("--database-url", default=None, help="Postgres DSN override.")

    rl = ssub.add_parser("release", help="Lift a host's reserve (§B-2).")
    rl.add_argument(
        "--host", default=None, help="Host to release (default: this host)."
    )
    rl.add_argument(
        "--all", action="store_true", help="Release the '*' (all-hosts) reserve."
    )
    rl.add_argument("--database-url", default=None, help="Postgres DSN override.")


def _refuse_reserve_service(name: str) -> None:
    """Exit(2) when a generic verb targets the ``reserve`` pseudo-service.

    ``reserve`` rows are only valid through ``precis service reserve`` /
    ``release`` (``set_reserve`` enforces the required ``expires_at`` +
    hour bounds). The generic UPSERTs don't set ``expires_at``, so a
    ``service prio <host> reserve N`` would mint an inert row — or mutate
    ``prio`` on a live reserve without touching what actually gates it
    (``reserve_active`` reads only ``expires_at``). One door.
    """
    if name == RESERVE_SERVICE:
        print(
            f"service: {RESERVE_SERVICE!r} is the reserve-mode pseudo-service "
            "— use 'precis service reserve' / 'precis service release', not "
            "the generic verbs",
            file=sys.stderr,
        )
        sys.exit(2)


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
    # `expires_at` is only shown when at least one row carries one (§B-2
    # reserve rows are the only writer today) — keeps the common table
    # narrow when nothing is TTL'd.
    has_expiry = any(r.get("expires_at") for r in rows)
    if has_expiry:
        print(
            f"{'host':<14} {'service':<20} {'prio':>4}  {'model_pref':<24} "
            f"{'expires_at':<26} actor"
        )
        for r in rows:
            print(
                f"{r['host']!s:<14} {r['service']!s:<20} "
                f"{r['prio']!s:>4}  {r['model_pref'] or '-'!s:<24} "
                f"{r['expires_at'] or '-'!s:<26} {r['actor'] or '-'}"
            )
        return
    print(f"{'host':<14} {'service':<20} {'prio':>4}  {'model_pref':<24} actor")
    for r in rows:
        print(
            f"{r['host']!s:<14} {r['service']!s:<20} "
            f"{r['prio']!s:>4}  {r['model_pref'] or '-'!s:<24} "
            f"{r['actor'] or '-'}"
        )


def _cmd_prio(store: Store, args: argparse.Namespace) -> None:
    _refuse_reserve_service(args.service)
    _warn_unknown_service(args.service)
    set_service_prio(store, args.host, args.service, args.prio, actor=args.actor)
    state = "OFF" if args.prio == 0 else f"weight {args.prio}"
    print(f"service_config: {args.host}/{args.service} → prio {args.prio} ({state})")


def _cmd_model(store: Store, args: argparse.Namespace) -> None:
    _refuse_reserve_service(args.service)
    _warn_unknown_service(args.service)
    model = None if args.clear else args.model
    set_service_model(store, args.host, args.service, model, actor=args.actor)
    shown = "(cleared)" if model is None else model
    print(f"service_config: {args.host}/{args.service} model_pref → {shown}")


def _cmd_clear(store: Store, args: argparse.Namespace) -> None:
    _refuse_reserve_service(args.service)
    removed = clear_service_config(store, args.host, args.service)
    if removed:
        print(f"service_config: removed {args.host}/{args.service}")
    else:
        print(f"service_config: no row for {args.host}/{args.service}")


def _cmd_seed(store: Store, args: argparse.Namespace) -> None:
    _refuse_reserve_service(args.service)
    _warn_unknown_service(args.service)
    inserted = seed_service_prio(
        store, args.host, args.service, args.prio, actor=args.actor
    )
    if inserted:
        print(f"service_config: seeded {args.host}/{args.service} -> prio {args.prio}")
    else:
        print(
            f"service_config: {args.host}/{args.service} already has a row "
            "— left untouched"
        )


def _reserve_target_host(args: argparse.Namespace) -> str:
    """``--all`` -> the ``*`` wildcard; else ``--host`` or this host's own
    identity — the SAME ``host_name()`` the claim gate resolves via
    ``reserve_host()`` (``PRECIS_HOST_NAME`` or the hostname), so an
    operator running this on the box they mean to reserve can't diverge
    from what the claim actually checks."""
    return ALL_HOSTS if args.all else (args.host or host_name())


def _cmd_reserve(store: Store, args: argparse.Namespace) -> None:
    host = _reserve_target_host(args)
    expires_at = set_reserve(store, host, hours=args.hours, actor=args.actor)
    print(
        f"service_config: {host} reserved until {expires_at} "
        f"({args.hours:g}h) — new ssh_node/claude_docker claims stop there "
        "immediately; in-flight jobs finish normally"
    )


def _cmd_release(store: Store, args: argparse.Namespace) -> None:
    host = _reserve_target_host(args)
    removed = clear_reserve(store, host)
    if removed:
        print(f"service_config: released reserve on {host}")
    else:
        print(f"service_config: no reserve row for {host}")


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
        elif args.service_cmd == "seed":
            _cmd_seed(store, args)
        elif args.service_cmd == "reserve":
            _cmd_reserve(store, args)
        elif args.service_cmd == "release":
            _cmd_release(store, args)
        else:  # pragma: no cover — argparse `required=True` guards this
            raise SystemExit(f"unknown service subcommand: {args.service_cmd!r}")
    finally:
        store.close()


__all__ = ["add_parser", "run"]

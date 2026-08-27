"""``precis pcb`` — PCB catalog maintenance.

Subcommands:

* ``refresh-parts`` — load/refresh the ``parts`` catalog (gr264357: prod
  ``parts`` was EMPTY — the parsing layer in ``precis.pcb.catalog`` existed
  but nothing ever called it; there was no CLI verb and no worker pass).
  See ``docs/backlog/pcb-guided-place-route.md`` "Footprint + catalog
  reality" for the full context.

Two sources, not equivalent:

* ``--from-api`` (preferred) — the JLCPCB Open API's ``lastKey`` cursor walk
  (:func:`precis.workers.parts_refresh.run_parts_refresh_pass`, sharing its
  checkpoint with the standing daily ``parts_refresh`` worker pass), a real
  incremental bulk pull. 403s get a clean, actionable message
  (:class:`~precis.pcb.jlc_api.JlcPermissionError`) until the app's Open API
  console is granted the Components scope — a human console action, not a
  bug. Any other vendor failure (outage, rate limiting, the politeness
  circuit breaker open — :mod:`precis.pcb._http`) also gets a clean
  operator-facing message, never a raw traceback, pointing at
  ``--from-sqlite PATH`` as the fallback.
* ``--from-sqlite PATH`` — the community yaqwsx/jlcparts SQLite dump (that
  project's publish format, not ours) via staging + atomic swap
  (:func:`precis.pcb.catalog.bulk_refresh_parts_from_sqlite`) — the bulk
  reload path for the whole ~300k-row catalog.

With neither flag: try the API, and fall back to the dump (``--from-sqlite``
or ``$PRECIS_JLCPARTS_DUMP_PATH``) when no credentials are configured.
Either way, the command prints which source it used.
"""

from __future__ import annotations

import argparse
import os
from typing import TYPE_CHECKING

from precis.cli._common import resolve_dsn

if TYPE_CHECKING:
    from precis.store import Store


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``pcb`` subcommand (currently: ``refresh-parts``)."""
    p = sub.add_parser(
        "pcb",
        help="PCB catalog maintenance (JLCPCB parts ingest).",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    psub = p.add_subparsers(dest="pcb_cmd", required=True)

    rp = psub.add_parser(
        "refresh-parts",
        help="Load/refresh the `parts` catalog from JLCPCB.",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = rp.add_mutually_exclusive_group()
    src.add_argument(
        "--from-api",
        action="store_true",
        help="Force the JLCPCB Open API `lastKey` cursor walk. Needs vault "
        "secrets JLCPCB_APP_ID / JLCPCB_ACCESS_KEY / JLCPCB_SECRET_KEY and "
        "the Components API scope granted in the JLCPCB Open API console.",
    )
    src.add_argument(
        "--from-sqlite",
        default=None,
        metavar="PATH",
        help="Force the community yaqwsx/jlcparts SQLite dump at PATH "
        "(staging + atomic swap). Default fallback when neither flag is "
        "given and no API credentials are configured: "
        "$PRECIS_JLCPARTS_DUMP_PATH.",
    )
    rp.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="Rows per API page (--from-api only; default 100).",
    )
    rp.add_argument(
        "--row-limit",
        type=int,
        default=None,
        help="Stop the API walk after this many rows (--from-api only; "
        "default: the standing pass's per-cycle row budget).",
    )
    rp.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )
    rp.set_defaults(func=run)
    return p


def run(args: argparse.Namespace) -> None:
    """Dispatch ``precis pcb <cmd>``."""
    if args.pcb_cmd == "refresh-parts":
        _refresh_parts(args)


def _refresh_parts(args: argparse.Namespace) -> None:
    from precis.store import Store

    dsn = resolve_dsn(getattr(args, "database_url", None))
    store = Store.connect(dsn)
    try:
        if args.from_sqlite:
            _refresh_from_sqlite(store, args.from_sqlite)
        elif args.from_api:
            _refresh_from_api(store, page_size=args.page_size, row_limit=args.row_limit)
        else:
            _refresh_default(store, page_size=args.page_size, row_limit=args.row_limit)
    finally:
        store.close()


def _refresh_from_sqlite(store: Store, path: str) -> None:
    from precis.pcb.catalog import bulk_refresh_parts_from_sqlite

    counts = bulk_refresh_parts_from_sqlite(store, path)
    print(
        f"pcb refresh-parts: source=jlcparts-dump path={path!r} "
        f"loaded={counts['loaded']} restocked={counts['restocked']}"
    )


def _refresh_from_api(store: Store, *, page_size: int, row_limit: int | None) -> None:
    from precis.pcb.jlc_api import JlcApiClient
    from precis.workers.parts_refresh import DEFAULT_ROW_BUDGET, run_parts_refresh_pass

    client = JlcApiClient(store=store)
    if not client.available:
        raise SystemExit(
            "pcb refresh-parts --from-api: no JLCPCB API credentials "
            "configured (vault secrets JLCPCB_APP_ID / JLCPCB_ACCESS_KEY / "
            "JLCPCB_SECRET_KEY) — use --from-sqlite PATH instead, or rerun "
            "with neither flag to fall back automatically."
        )
    result = run_parts_refresh_pass(
        store,
        row_budget=row_limit if row_limit is not None else DEFAULT_ROW_BUDGET,
        client=client,
        page_size=page_size,
    )
    error = result.get("error")
    if error:
        # A clean operator-facing message, not a traceback — see
        # precis.pcb.jlc_api.JlcPermissionError's module docstring for the
        # 403 case; a plainer failure here (run_parts_refresh_pass also
        # catches precis.pcb._http.VendorError/VendorUnavailable — an
        # outage, rate limiting, or the politeness circuit breaker open) is
        # just as clean, and either way --from-sqlite PATH is the fallback.
        raise SystemExit(
            f"pcb refresh-parts --from-api: {error} — use --from-sqlite "
            "PATH instead, or retry once the issue clears."
        )
    print(
        f"pcb refresh-parts: source=jlcpcb-api rows={result['claimed']} "
        f"upserted={result['ok']} restocked={result.get('restocked', 0)}"
    )


def _refresh_default(store: Store, *, page_size: int, row_limit: int | None) -> None:
    from precis.pcb.jlc_api import JlcApiClient

    client = JlcApiClient(store=store)
    if client.available:
        _refresh_from_api(store, page_size=page_size, row_limit=row_limit)
        return
    path = os.environ.get("PRECIS_JLCPARTS_DUMP_PATH")
    if not path:
        raise SystemExit(
            "pcb refresh-parts: no JLCPCB API credentials configured, and "
            "no dump path to fall back to — pass --from-sqlite PATH, set "
            "$PRECIS_JLCPARTS_DUMP_PATH, or configure the JLCPCB_APP_ID / "
            "JLCPCB_ACCESS_KEY / JLCPCB_SECRET_KEY vault secrets."
        )
    print(
        "pcb refresh-parts: no JLCPCB API credentials configured; "
        f"falling back to the community dump at {path!r}"
    )
    _refresh_from_sqlite(store, path)


__all__ = ["add_parser", "run"]

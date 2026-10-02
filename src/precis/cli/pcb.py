"""``precis pcb`` — PCB catalog maintenance and board import.

Subcommands:

* ``import-epro`` — import an EasyEDA Pro ``.epro2`` board into the ``pcb``
  kind (``docs/backlog/pcb-epro-import.md``). A CLI verb rather than the
  MCP ``put`` surface because a real board is hundreds of pads, and
  marshalling that through JSON arguments is the wrong pipe. ``--dry-run``
  reads, derives and refuses exactly as the real import would, then writes
  nothing — so the warnings can be read before committing to a board.
  ``--update`` re-imports onto an existing import of the same board
  (moves and new parts applied, everything else reported). Prints the
  copper report too (below).
* ``copper-report`` — the source board's own track widths, via sizes and
  gaps beside the rules precis resolves for each net
  (:mod:`precis.pcb.copper_report`). Re-run after annotating nets to see
  which disagreements the annotations resolved. Writes nothing.
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
import sys
from typing import TYPE_CHECKING

from precis.cli._common import resolve_dsn

if TYPE_CHECKING:
    from precis.store import Store


#: ``import-epro`` exit codes. Named because a script around this command
#: needs to tell "your file is fine, precis cannot take it yet" (a layer
#: count, an existing slug) apart from "this file is not readable" — the
#: first is worth waiting for, the second is worth re-exporting.
EXIT_UNREADABLE = 2
EXIT_REFUSED = 3


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``pcb`` subcommand (``import-epro``,
    ``refresh-parts``)."""
    p = sub.add_parser(
        "pcb",
        help="PCB board import and catalog maintenance.",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    psub = p.add_subparsers(dest="pcb_cmd", required=True)

    ie = psub.add_parser(
        "import-epro",
        help="Import an EasyEDA Pro .epro2 board into the pcb kind.",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ie.add_argument("path", help="Path to the .epro2 file.")
    ie.add_argument(
        "--slug",
        required=True,
        help="The pcb slug to create. Refuses if it already exists unless "
        "--update is given: a plain second import would EXTEND the design "
        "(an existing refdes keeps its old position, and features have no "
        "dedup key at all).",
    )
    ie.add_argument(
        "--update",
        action="store_true",
        help="Re-import onto an existing import of the SAME board: moved "
        "parts take the source's pose, new parts are added; a removed "
        "part, a rewired pin or a changed outline is reported, not "
        "applied (precis is where the design is being corrected).",
    )
    ie.add_argument(
        "--title",
        default=None,
        help="Design title (default: the project's own title).",
    )
    ie.add_argument(
        "--board",
        default=None,
        metavar="UUID",
        help="Which PCB document to import. Required when the project "
        "carries more than one — not theoretical: a real export was seen "
        "holding seven, with the interesting board LAST.",
    )
    ie.add_argument(
        "--dry-run",
        action="store_true",
        help="Read, derive and refuse exactly as a real import would, then "
        "write nothing. Reports identical counts and warnings.",
    )
    ie.add_argument(
        "--unfrozen",
        action="store_true",
        help="Import parts unlocked. By default every part is imported "
        "locked (fixed='both'), so op='route' routes the board as placed "
        "instead of re-placing it first.",
    )
    ie.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )
    ie.set_defaults(func=run)

    cr = psub.add_parser(
        "copper-report",
        help="Measure an imported .epro2 board's own copper against the "
        "slug's current net rules.",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    cr.add_argument("path", help="The .epro2 file the slug was imported from.")
    cr.add_argument("--slug", required=True, help="The imported pcb slug.")
    cr.add_argument("--board", default=None, metavar="UUID", help="As import-epro.")
    cr.add_argument(
        "--database-url", default=None, help="Override PRECIS_DATABASE_URL."
    )
    cr.set_defaults(func=run)

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
    if args.pcb_cmd == "import-epro":
        _import_epro(args)
    elif args.pcb_cmd == "copper-report":
        _copper_report(args)
    elif args.pcb_cmd == "refresh-parts":
        _refresh_parts(args)


def _import_epro(args: argparse.Namespace) -> None:
    import pathlib

    from precis.ingest.pcb_epro import EproImportError, import_epro
    from precis.pcb import copper_report
    from precis.pcb.epro import EproError
    from precis.store import Store

    path = pathlib.Path(args.path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SystemExit(f"pcb import-epro: cannot read {args.path!r}: {exc}") from exc

    dsn = resolve_dsn(getattr(args, "database_url", None))
    store = Store.connect(dsn)
    try:
        result = import_epro(
            store,
            data,
            slug=args.slug,
            title=args.title,
            board_uuid=args.board,
            source_name=path.name,
            dry_run=args.dry_run,
            update=getattr(args, "update", False),
            freeze=not getattr(args, "unfrozen", False),
        )
    except EproError as exc:
        # The file is not readable as .epro2, or is missing something
        # precis cannot invent (an outline). Re-exporting might fix it.
        print(f"pcb import-epro: cannot read this board — {exc}", file=sys.stderr)
        raise SystemExit(EXIT_UNREADABLE) from exc
    except EproImportError as exc:
        # The file is fine; precis will not take it as it stands.
        print(f"pcb import-epro: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_REFUSED) from exc
    finally:
        store.close()

    if result.update is not None:
        what = "would update" if args.dry_run else "updated"
    else:
        what = "would import" if args.dry_run else "imported"
    s = result.stats
    print(
        f"pcb import-epro: {what} {args.slug!r} — "
        f"{s['components']} component(s), {s['footprints']} footprint(s), "
        f"{s['nets']} net(s), {s['connections']} connection(s), "
        f"{s['mounting_holes']} mounting hole(s)"
    )
    print(f"  stackup: {', '.join(_layer_label(layer) for layer in result.stackup)}")
    if result.planes:
        planes = ", ".join(f"{k}={v}" for k, v in sorted(result.planes.items()))
        print(f"  planes:  {planes}")
    # Every warning, always, and never a count standing in for them: each
    # one is a thing the imported board does NOT carry, and a user who
    # cannot see which will assume it came across.
    for w in result.warnings:
        print(f"  warn: {w}")
    if result.update is not None:
        for line in result.update.lines():
            print(f"  {line}")
    if result.copper is not None:
        for line in copper_report.render(result.copper):
            print(f"  {line}")
    if args.dry_run:
        print("  (dry run — nothing was written)")


def _layer_label(layer: dict[str, object]) -> str:
    """``In1.Cu (plane, routable)`` — a plane the router may also route on
    is the case a bare name would hide."""
    if layer.get("role") != "plane":
        return str(layer["name"])
    routable = ", routable" if layer.get("routable") else ""
    return f"{layer['name']} (plane{routable})"


def _copper_report(args: argparse.Namespace) -> None:
    import pathlib

    from precis.ingest.pcb_epro import EproImportError, report_copper
    from precis.pcb import copper_report
    from precis.pcb.epro import EproError
    from precis.store import Store

    try:
        data = pathlib.Path(args.path).read_bytes()
    except OSError as exc:
        raise SystemExit(
            f"pcb copper-report: cannot read {args.path!r}: {exc}"
        ) from exc
    store = Store.connect(resolve_dsn(getattr(args, "database_url", None)))
    try:
        rep = report_copper(store, data, slug=args.slug, board_uuid=args.board)
    except EproError as exc:
        print(f"pcb copper-report: cannot read this board — {exc}", file=sys.stderr)
        raise SystemExit(EXIT_UNREADABLE) from exc
    except EproImportError as exc:
        print(f"pcb copper-report: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_REFUSED) from exc
    finally:
        store.close()
    print(f"pcb copper-report: {args.slug!r}")
    for line in copper_report.render(rep):
        print(f"  {line}")


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

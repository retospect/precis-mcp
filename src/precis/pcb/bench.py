"""Developer entry point for the in-memory PCB acceptance boards.

``uv run python -m precis.pcb.bench sanity [variant ...]`` routes the
sanity board variants (:mod:`precis.pcb.sanity_board`) and prints
routed/20, DRC by rule and the per-net verdicts; exit 1 unless every
variant is complete with 0 DRC errors. No store, no DB.

``uv run python -m precis.pcb.bench <example-board> [--seed N]`` routes
an ``examples/boards/<dir>/main.py`` board script (``build()``) through
the offline engine — :func:`precis.pcb.local.place` then
:func:`precis.pcb.local.route` with the settled poses held, the same
two-phase path the place/route jobs run. A partial name resolves to a
unique board dir (``nano`` → ``nano_usb_pd``). Catalog footprints come
from the matching ``tests/fixtures/pcb/<dir>_testboard`` module's
``CATALOG_FOOTPRINTS`` when the repo tree is present; without it the
lcsc parts fall to synthesized landpattern bounds. No store, no DB
(gr513867 — the single-board local entry for workspace boards).
"""

from __future__ import annotations

import sys
from pathlib import Path

from precis.pcb import sanity_board, sanity_power


def _boards_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "examples" / "boards"


def _resolve_board_dir(name: str) -> str | None:
    """``<dir>/main.py`` names under ``examples/boards``: exact match wins,
    else the shortest dir starting with ``name`` (``nano`` →
    ``nano_usb_pd``, the base board — not the ``_r2`` revision)."""
    boards_dir = _boards_dir()
    if not boards_dir.is_dir():
        return None
    dirs = sorted(p.name for p in boards_dir.iterdir() if (p / "main.py").is_file())
    match = [d for d in dirs if d == name] or [d for d in dirs if d.startswith(name)]
    return min(match, key=len) if match else None


def _example_board(name: str, args: list[str]) -> int:
    import importlib
    import importlib.util
    import time

    from precis.pcb import local, route_digest

    seed = 1
    it = iter(args)
    for arg in it:
        if arg == "--seed":
            seed = int(next(it))
        else:
            print(f"ERR: unknown flag {arg!r}", file=sys.stderr)
            return 2

    dirname = _resolve_board_dir(name)
    if dirname is None:
        dirs = (
            sorted(p.name for p in _boards_dir().iterdir())
            if _boards_dir().is_dir()
            else []
        )
        print(
            f"ERR: no examples/boards/<dir> matching {name!r} — have: {', '.join(dirs)}",
            file=sys.stderr,
        )
        return 2

    spec = importlib.util.spec_from_file_location(
        f"examples_boards_{dirname}", _boards_dir() / dirname / "main.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    board = mod.build()

    catalog = None
    try:
        fixture = importlib.import_module(f"tests.fixtures.pcb.{dirname}_testboard")
        catalog = fixture.CATALOG_FOOTPRINTS
    except Exception:
        print(
            f"note: no catalog footprint fixture for {dirname} — "
            "lcsc parts get synthesized landpattern bounds"
        )

    try:
        t0 = time.monotonic()
        placement = local.place(board, seed=seed, catalog=catalog)
        place_s = time.monotonic() - t0
        print(placement.summary())
        t0 = time.monotonic()
        routing = local.route(board, placement=placement, seed=seed, catalog=catalog)
        route_s = time.monotonic() - t0
    except Exception as exc:
        print(f"ERR: {exc}", file=sys.stderr)
        return 2

    print(routing.summary())
    for net in routing.failed_nets():
        print(f"  {net}: {routing.rows[net].get('note')}")
    for line in route_digest.digest_lines(routing.digest):
        print(line)
    print(f"place {place_s:.1f}s · route {route_s:.1f}s")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    boards = {"sanity": sanity_board.main, "power": sanity_power.main}
    if args and args[0] in boards:
        return boards[args[0]](args[1:])
    if args and not args[0].startswith("-"):
        return _example_board(args[0], args[1:])
    print(
        "usage: python -m precis.pcb.bench sanity|power|<example-board> "
        "[variant ...] [--seed N]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

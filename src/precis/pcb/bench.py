"""Developer entry point for the in-memory PCB acceptance boards.

``uv run python -m precis.pcb.bench sanity [variant ...]`` routes the
sanity board variants (:mod:`precis.pcb.sanity_board`) and prints
routed/20, DRC by rule and the per-net verdicts; exit 1 unless every
variant is complete with 0 DRC errors. No store, no DB.
"""

from __future__ import annotations

import sys

from precis.pcb import sanity_board


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "sanity":
        print("usage: python -m precis.pcb.bench sanity [variant ...]", file=sys.stderr)
        return 2
    return sanity_board.main(args[1:])


if __name__ == "__main__":
    raise SystemExit(main())

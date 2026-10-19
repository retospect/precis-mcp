"""``precis pcb-bench`` — the in-memory PCB acceptance boards, as a
subcommand, so ``scripts/prod-precis pcb-bench sanity --upload`` takes the
normal prod DSN path. A thin pass-through over :mod:`precis.pcb.bench`."""

from __future__ import annotations

import argparse

_HELP = "Route the PCB sanity boards in memory; optional --svg, --upload."


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p = sub.add_parser(
        "pcb-bench",
        help=_HELP,
        description=(
            "precis pcb-bench sanity [variant ...] [--svg DIR] [--upload "
            "[--version]]. Routes each variant in memory and prints routed/20, "
            "DRC by rule and per-net verdicts. --upload puts the routed board "
            "in the configured store as a frozen sanity-<variant> board and "
            "NEVER replaces one: a slug that already exists is refused with a "
            "suggested sanity-<variant>-v2 (pass --version to take it). That "
            "refusal is correct behaviour; boards placed and routed by the "
            "cluster jobs already hold the plain slugs on prod."
        ),
    )
    p.add_argument(
        "bench_args",
        nargs=argparse.REMAINDER,
        help="Arguments of `python -m precis.pcb.bench`, starting with `sanity`.",
    )
    return p


def run(args: argparse.Namespace) -> None:
    from precis.pcb import bench

    raise SystemExit(bench.main(list(args.bench_args)))

"""``precis-dft-run`` — the container's command dispatcher.

One console entry point, subcommands per workload. The host side
(`precis_dft.jobs.gpaw_relax.build_docker_argv`) invokes this as
``precis-dft-run gpaw-relax --in /work/in --out /work/out``.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="precis-dft-run")
    sub = parser.add_subparsers(dest="cmd", required=True)

    relax = sub.add_parser("gpaw-relax", help="relax a structure with GPAW")
    relax.add_argument("--in", dest="in_dir", required=True)
    relax.add_argument("--out", dest="out_dir", required=True)

    args = parser.parse_args(argv)

    if args.cmd == "gpaw-relax":
        from precis_dft._container.gpaw_relax import run_cli

        return run_cli(args.in_dir, args.out_dir)
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

"""Single CLI entry point: ``precis serve | migrate | jobs ...``.

Subcommands:
    serve     Run the MCP server on stdio.
    migrate   Apply pending DB migrations (phase 2+; placeholder).
    jobs      Run a background job (phase 2+; placeholder).
"""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="precis",
        description="precis-mcp v2 — paper, document, state, and tool access.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="Run the MCP server (stdio).")
    sub.add_parser("migrate", help="Apply pending DB migrations (phase 2+).")
    jobs = sub.add_parser("jobs", help="Run a background job (phase 2+).")
    jobs.add_argument("name", help="Job name.")
    jobs.add_argument("args", nargs="*", help="Job arguments.")

    args = parser.parse_args()

    if args.cmd == "serve":
        from precis.server import main as serve

        serve()
        return

    if args.cmd == "migrate":
        print("migrate: not yet implemented (phase 2)", file=sys.stderr)
        sys.exit(2)

    if args.cmd == "jobs":
        print(f"jobs: {args.name!r} not yet implemented (phase 2+)", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()

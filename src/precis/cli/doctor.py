"""``precis doctor`` — read-only install verification.

Prints one line per check (``ok`` / ``warn`` / ``fail`` plus the next
command on failure) and exits non-zero when any check fails. The check
engine is :mod:`precis.doctor` — the web ``/status`` Setup tab renders
the same table, so the two surfaces cannot drift.
"""

from __future__ import annotations

import argparse

from precis.doctor import FAIL, has_failures, run_checks

#: Column widths for the one-line-per-check report.
_LEVEL_PAD = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``doctor`` subparser."""
    parser = sub.add_parser(
        "doctor",
        help="Verify a precis install (read-only; non-zero exit on failure).",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )
    parser.add_argument(
        "--roles",
        action="store_true",
        help="Also assert the tiered DB roles exist (deployments that ran "
        "precis setup --roles).",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Run the provider probes for configured secrets "
        "(precis.secret_status; default is presence-only).",
    )
    return parser


def run(args: argparse.Namespace) -> None:
    """Run the sweep and print the one-line-per-check report."""
    checks = run_checks(
        dsn=getattr(args, "database_url", None),
        roles=getattr(args, "roles", False),
        probe=getattr(args, "probe", False),
    )
    for c in checks:
        line = f"{_LEVEL_PAD[c.level]}  {c.name:<11} {c.detail}".rstrip()
        if c.level == FAIL and c.next:
            line += f" — next: {c.next}"
        print(line)
    raise SystemExit(1 if has_failures(checks) else 0)


__all__ = ["add_parser", "run"]

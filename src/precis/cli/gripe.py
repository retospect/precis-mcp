"""``precis gripes`` — DEPRECATED dump of filed gripes.

Was the v0 human-triage path when ``gripe`` was a write-only kind
on the MCP surface. As of migration 0005 gripe is a full
first-class kind — ``get(kind='gripe', id=N)`` reads the body +
comment timeline, ``search(kind='gripe', q=...)`` finds matches
by text or tag, ``tag``/``link``/``delete`` work normally. This
CLI duplicates that surface from the terminal and will be
removed in a follow-up release.

The implementation is preserved verbatim so existing shell
scripts keep working; only the entry point prints a one-line
deprecation notice to stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any

from precis.cli._common import resolve_dsn


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``gripes`` subparser on ``sub``."""
    dp = sub.add_parser(
        "gripes",
        help="Dump all filed gripes for human triage.",
        description=(
            "Dump every filed gripe. The gripe kind is write-only from "
            "the agent surface; this CLI is the human read/triage path."
        ),
    )
    dp.add_argument("--database-url", default=None)
    dp.add_argument(
        "--include-deleted",
        action="store_true",
        help=(
            "Also print soft-deleted gripes (tombstones retained for "
            "audit are normally hidden)."
        ),
    )
    dp.add_argument(
        "--only-deleted",
        action="store_true",
        help="Print only soft-deleted gripes (implies --include-deleted).",
    )
    dp.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    dp.add_argument(
        "--oldest-first",
        action="store_true",
        help="Walk the backlog in filed order (default: newest first).",
    )
    dp.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of gripes printed (default: no limit).",
    )
    return dp


def run(args: argparse.Namespace) -> None:
    """Implements ``precis gripes``."""
    from precis.config import load_config
    from precis.store import Store

    print(
        "precis gripes: DEPRECATED — gripe is now a full MCP kind. "
        "Use get(kind='gripe', id=N) / search(kind='gripe', q=...). "
        "This CLI will be removed in a follow-up release.",
        file=sys.stderr,
    )

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)

    include_deleted = args.include_deleted or args.only_deleted

    clauses = ["kind = %s"]
    params: list[Any] = ["gripe"]
    if args.only_deleted:
        clauses.append("deleted_at IS NOT NULL")
    elif not include_deleted:
        clauses.append("deleted_at IS NULL")

    order = "ASC" if args.oldest_first else "DESC"
    sql = (
        "SELECT ref_id, title, created_at, updated_at, deleted_at "
        "FROM refs WHERE " + " AND ".join(clauses) + f" ORDER BY ref_id {order}"
    )
    if args.limit is not None:
        if args.limit < 0:
            print("--limit must be >= 0", file=sys.stderr)
            sys.exit(2)
        sql += " LIMIT %s"
        params.append(args.limit)

    store = Store.connect(dsn)
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()

        if args.format == "json":
            for row in rows:
                print(json.dumps(_row_to_json(row)))
            return

        # text format
        if not rows:
            scope = "gripes (incl. deleted)" if include_deleted else "live gripes"
            print(f"no {scope} on file")
            return

        total_live = sum(1 for r in rows if r[4] is None)
        total_deleted = len(rows) - total_live
        header_parts = [f"{len(rows)} gripe(s)"]
        if include_deleted:
            header_parts.append(f"live={total_live}")
            header_parts.append(f"deleted={total_deleted}")
        print(f"# {' '.join(header_parts)}")
        print()
        for row in rows:
            _print_text(row)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Row formatters
# ---------------------------------------------------------------------------


def _row_to_json(row: tuple[Any, ...]) -> dict[str, Any]:
    ref_id, title, created_at, updated_at, deleted_at = row
    out: dict[str, Any] = {
        "id": int(ref_id),
        "text": title,
        "created_at": _iso(created_at),
        "updated_at": _iso(updated_at),
    }
    if deleted_at is not None:
        out["deleted_at"] = _iso(deleted_at)
    return out


def _print_text(row: tuple[Any, ...]) -> None:
    ref_id, title, created_at, _updated_at, deleted_at = row
    marker = "  (deleted)" if deleted_at is not None else ""
    print(f"## gripe {ref_id}  [{_iso(created_at)}]{marker}")
    print(title)
    print()


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


__all__ = ["add_parser", "run"]

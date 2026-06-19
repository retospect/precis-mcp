"""``precis stats`` — quick observability summaries.

Two summaries, picked via flags:

* ``--findings`` (default) — counts of findings per ``STATUS:``
  value. Surfaces "are my chases progressing?" at a glance:
  ``tracing`` rows are in-flight, ``established`` rows are done,
  ``multi_candidate`` / ``dead_chain`` rows want operator
  attention.
* ``--stubs`` — count of stub paper refs (``pdf_sha256 IS NULL``)
  outstanding. Complements ``precis stubs`` (which lists the
  backlog row-by-row); this command answers "how big is the
  backlog?" without dumping it.

Both flags can be combined to print both sections.

Sibling commands:

* ``precis stubs`` — row-level stub listing.
* ``precis worker --only chase`` / ``--only fetch`` — drive the
  workers that empty each backlog.
"""

from __future__ import annotations

import argparse
from typing import Any

from precis.cli._common import (
    add_format_argument,
    resolve_dsn,
    resolve_format,
)
from precis.format import serialize
from precis.store import Store

# Pinned column order for both halves. Adding a column lands in
# one place so TOON / JSON / table all stay in sync.
_FINDINGS_SCHEMA: list[str] = ["status", "count"]
_STUBS_SCHEMA: list[str] = ["state", "count"]


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "stats",
        help="Summarise finding-status counts and stub backlog.",
        description=(
            "Surface quick observability summaries. ``--findings`` "
            "shows counts per STATUS: value (tracing / established / "
            "multi_candidate / dead_chain). ``--stubs`` shows the "
            "stub paper backlog (PDFs the chase worker wants but "
            "doesn't have yet). Default: print both sections."
        ),
    )
    p.add_argument(
        "--findings",
        action="store_true",
        help="Show STATUS-count summary for kind='finding'.",
    )
    p.add_argument(
        "--stubs",
        action="store_true",
        help="Show count of stub paper refs (pdf_sha256 IS NULL).",
    )
    p.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )
    add_format_argument(p)


def run(args: argparse.Namespace) -> None:
    # No flags = print both. A flag toggles inclusion of just
    # that section so the operator can pipe one summary to a
    # downstream filter without the other muddying it up.
    show_findings = args.findings or not (args.findings or args.stubs)
    show_stubs = args.stubs or not (args.findings or args.stubs)

    dsn = resolve_dsn(args.database_url)
    fmt = resolve_format(args)

    sections: list[tuple[str, list[str], list[dict[str, Any]]]] = []
    store = Store.connect(dsn)
    try:
        if show_findings:
            sections.append(("findings", _FINDINGS_SCHEMA, _query_findings(store)))
        if show_stubs:
            sections.append(("stubs", _STUBS_SCHEMA, _query_stubs(store)))
    finally:
        store.close()

    if fmt == "json":
        # Single JSON object keyed by section name so callers piping
        # to ``jq`` can pick one half: ``precis stats --format json
        # | jq .findings``.
        import json

        payload = {name: rows for name, _, rows in sections}
        print(json.dumps(payload, indent=2))
        return

    for i, (name, schema, rows) in enumerate(sections):
        if i:
            print()
        print(f"# {name}")
        if not rows:
            print("(empty)")
            continue
        print(serialize(rows, schema=schema, format=fmt))


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def _query_findings(store: Store) -> list[dict[str, Any]]:
    """STATUS-count summary for ``kind='finding'``.

    Rows whose STATUS tag is absent surface under
    ``status='(none)'`` so the count is exhaustive of the
    ``finding`` corpus (deleted refs excluded).
    """
    sql = (
        "WITH rows AS ("
        "  SELECT r.ref_id, "
        "         COALESCE(t.value, '(none)') AS status "
        "  FROM refs r "
        "  LEFT JOIN ref_tags rt ON rt.ref_id = r.ref_id "
        "  LEFT JOIN tags t ON t.tag_id = rt.tag_id "
        "     AND t.namespace = 'STATUS' "
        "  WHERE r.kind = 'finding' AND r.deleted_at IS NULL"
        ") "
        "SELECT status, count(*)::int AS count "
        "FROM rows "
        "GROUP BY status "
        "ORDER BY count DESC, status ASC"
    )
    with store.pool.connection() as conn:
        cur = conn.execute(sql)
        return [{"status": r[0], "count": int(r[1])} for r in cur.fetchall()]


def _query_stubs(store: Store) -> list[dict[str, Any]]:
    """Stub backlog summary.

    Two states surface:

    * ``awaiting`` — stub created, never fetched (no
      ``ref_events.source LIKE 'fetcher:%'`` row).
    * ``retry`` — stub was attempted at least once and still has
      no PDF; ripe for the next fetch pass.

    A stub that has a PDF (``pdf_sha256 IS NOT NULL``) is no
    longer a stub and falls out of the count.
    """
    sql = (
        "SELECT CASE "
        "         WHEN last_event.source IS NULL THEN 'awaiting' "
        "         ELSE 'retry' "
        "       END AS state, "
        "       count(*)::int AS count "
        "FROM refs r "
        "LEFT JOIN LATERAL ( "
        "  SELECT source FROM ref_events "
        "  WHERE ref_id = r.ref_id AND source LIKE 'fetcher:%' "
        "  ORDER BY ts DESC LIMIT 1 "
        ") last_event ON TRUE "
        "WHERE r.kind = 'paper' "
        "  AND r.pdf_sha256 IS NULL "
        "  AND r.deleted_at IS NULL "
        "GROUP BY state "
        "ORDER BY state ASC"
    )
    with store.pool.connection() as conn:
        cur = conn.execute(sql)
        return [{"state": r[0], "count": int(r[1])} for r in cur.fetchall()]


__all__ = ["add_parser", "run"]

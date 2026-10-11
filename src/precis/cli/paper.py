"""``precis paper …`` — operations on the ``paper`` kind.

Subcommands:

* ``precis paper authors-resplit [--limit N] [--dry-run]`` — one-time
  post-deploy pass (precis.utils.authors module docstring): migration
  0168's SQL backfill projected every legacy ``refs.authors`` byline
  into ``paper_authors`` (``source='legacy'``), but SQL can't apply the
  Python split heuristics (:func:`precis.utils.authors.split_middle`,
  the junk guard, initials tidying). This re-runs
  ``store.set_paper_authors(source='legacy')`` over every ref that still
  has a ``source='legacy'`` row, which re-derives ``middle`` and tidies
  the split. Idempotent — a fully-resplit row stays ``'legacy'``, so
  re-running is a no-op.
* ``precis paper suspect-previews [--limit N]`` — read-only list of live
  article papers whose extracted ``pdf_pages`` span is <= 2 (the signature in
  ``docs/runbooks/elsevier-preview-pdf-remediation.md``): ref id, title,
  pages, chunk count. Writes nothing.
"""

from __future__ import annotations

import argparse
import sys

from precis.cli._common import resolve_dsn
from precis.store import Store


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("paper", help="Operate on paper refs (authors-resplit, …).")
    psub = p.add_subparsers(dest="paper_cmd", required=True)

    ar = psub.add_parser(
        "authors-resplit",
        help="Re-split source='legacy' paper_authors rows (post-migration pass).",
        description=(
            "Re-run the Python author-splitting heuristics "
            "(split_middle + the junk guard) over every paper_authors "
            "row the 0168 migration's SQL backfill left as "
            "source='legacy'. Idempotent; safe to run repeatedly."
        ),
    )
    ar.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of refs processed (default: all).",
    )
    ar.add_argument(
        "--dry-run",
        action="store_true",
        help="Report how many refs would be touched without writing.",
    )
    ar.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )

    sp = psub.add_parser(
        "suspect-previews",
        help="List article papers whose PDF spans <= 2 pages (read-only).",
        description=(
            "List live papers with an article (or unknown) entry_type whose "
            "refs.pdf_pages range spans at most 2 pages - the publisher-preview "
            "signature. Prints ref id, title, pages and chunk count; writes nothing."
        ),
    )
    sp.add_argument("--limit", type=int, default=None, help="Cap rows listed.")
    sp.add_argument(
        "--database-url", default=None, help="Override PRECIS_DATABASE_URL."
    )


def run(args: argparse.Namespace) -> None:
    if args.paper_cmd == "authors-resplit":
        _run_authors_resplit(args)
        return
    if args.paper_cmd == "suspect-previews":
        _run_suspect_previews(args)
        return
    print(f"paper: unknown subcommand {args.paper_cmd!r}", file=sys.stderr)
    sys.exit(2)


def _run_authors_resplit(args: argparse.Namespace) -> None:
    dsn = resolve_dsn(args.database_url)
    store = Store.connect(dsn)
    try:
        n = resplit_legacy_authors(store, limit=args.limit, dry_run=args.dry_run)
        if args.dry_run:
            print(f"authors-resplit: dry-run — {n} paper(s) would be resplit")
        else:
            print(f"authors-resplit: resplit {n} paper(s)")
    finally:
        store.close()


def resplit_legacy_authors(
    store: Store, *, limit: int | None = None, dry_run: bool = False
) -> int:
    """The pass behind ``precis paper authors-resplit`` — exported so
    tests can call it directly.

    Selects every distinct ``ref_id`` with a ``source='legacy'``
    ``paper_authors`` row, plus every paper whose ``refs.authors`` is
    non-empty but has zero rows (shapes the migration's SQL projection
    skipped), then (unless ``dry_run``) re-runs
    ``store.set_paper_authors(ref_id, ref.authors, source='legacy')`` for
    each — the current ``refs.authors`` jsonb IS the table's own
    projection, so this just re-applies the Python split heuristics on
    top of it. Returns the count of refs selected (dry-run) or
    successfully re-projected.
    """
    # Two populations: rows the migration minted as 'legacy' (re-apply the
    # Python split on top), and papers the SQL projection could not mint
    # any row for — a semicolon-packed string, non-object elements — whose
    # jsonb is non-empty but whose table is empty (normalize_authors
    # handles every legacy shape).
    sql = """
        SELECT ref_id FROM paper_authors WHERE source = 'legacy'
        UNION
        SELECT r.ref_id FROM refs r
         WHERE r.kind = 'paper' AND r.retired_at IS NULL
           AND r.authors IS NOT NULL
           AND r.authors::text NOT IN ('[]', '""', 'null')
           AND NOT EXISTS (SELECT 1 FROM paper_authors pa WHERE pa.ref_id = r.ref_id)
        ORDER BY ref_id
    """
    params: tuple[object, ...] = ()
    if limit is not None:
        sql += " LIMIT %s"
        params = (limit,)
    with store.pool.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    ref_ids = [int(r[0]) for r in rows]
    if dry_run:
        return len(ref_ids)

    n = 0
    refs = store.fetch_refs_by_ids(ref_ids, include_deleted=False)
    for ref_id in ref_ids:
        ref = refs.get(ref_id)
        if ref is None:
            continue
        store.set_paper_authors(ref_id, ref.authors, source="legacy")
        n += 1
    return n


def suspect_previews(
    store: Store, *, limit: int | None = None
) -> list[tuple[int, str, int, int]]:
    """``(ref_id, title, pages, chunk_count)`` of live article papers whose
    ``pdf_pages`` span is <= 2. Read-only; the query behind
    ``precis paper suspect-previews``."""
    from precis.workers.job_types.paper_pull import _NON_ARTICLE_TYPES

    sql = """
        SELECT r.ref_id, coalesce(r.title, ''),
               upper(r.pdf_pages) - lower(r.pdf_pages) AS pages,
               (SELECT count(*) FROM chunks c WHERE c.ref_id = r.ref_id)
          FROM refs r
         WHERE r.kind = 'paper' AND r.retired_at IS NULL
           AND r.pdf_pages IS NOT NULL
           AND upper(r.pdf_pages) - lower(r.pdf_pages) <= 2
           AND NOT (lower(coalesce(r.meta->>'entry_type', '')) = ANY(%s))
         ORDER BY r.ref_id
    """
    params: tuple[object, ...] = (sorted(_NON_ARTICLE_TYPES),)
    if limit is not None:
        sql += " LIMIT %s"
        params += (limit,)
    with store.pool.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [(int(r[0]), str(r[1]), int(r[2]), int(r[3])) for r in rows]


def _run_suspect_previews(args: argparse.Namespace) -> None:
    store = Store.connect(resolve_dsn(args.database_url))
    try:
        rows = suspect_previews(store, limit=args.limit)
    finally:
        store.close()
    for ref_id, title, pages, chunks in rows:
        print(f"{ref_id}\tpages={pages}\tchunks={chunks}\t{title[:100]}")
    print(f"suspect-previews: {len(rows)} paper(s)")

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
* ``precis paper fetch-si <id>`` — stamp ``meta.si_fetch`` on one paper so
  the fetch lane's SI pass discovers, downloads and ingests its
  supplementary files as linked ``role='supplement'`` refs. The CLI
  spelling of ``put(kind='paper', id=…, mode='fetch-si')`` (stamps
  ``by='cli'``); ``<id>`` takes a cite_key slug, ``paNNN`` handle,
  numeric ref_id or DOI/identifier form.
* ``precis paper dump <id> --out <file>`` — the CLI twin of
  ``get(kind='paper', id=…, view='ops')`` (gr517423 slice 1): writes one
  ``precis-paper-dump/1`` JSON with every slice's chunks merged, for
  operator batch runs ahead of body repair doors.
* ``precis paper restore <file> [--apply] [--target new] [--handle s]``
  — the slice-2 twin: feeds one merged dump file back through
  ``paper_dump.restore_slice`` in a single transaction. A dry run unless
  ``--apply``; on write it appends the same ``paper_restore_slice`` /
  ``paper_restored`` ref_events the put door records (``source='cli'``).
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from precis.cli._common import REFUSAL_EXIT, resolve_dsn
from precis.errors import PrecisError
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

    fs = psub.add_parser(
        "fetch-si",
        help="Queue a supplementary-information fetch for one paper.",
        description=(
            "Stamp refs.meta.si_fetch so the fetch_oa lane's SI pass "
            "discovers, downloads and ingests the paper's supplementary "
            "files as linked role='supplement' refs - the CLI spelling of "
            "put(kind='paper', id=..., mode='fetch-si'), stamped by='cli'. "
            "The outcome lands on the paper (si_found / si_none / "
            "si_blocked / si_error)."
        ),
    )
    fs.add_argument(
        "id",
        help="Paper id: cite_key slug, paNNN handle, numeric ref_id or DOI.",
    )
    fs.add_argument(
        "--database-url", default=None, help="Override PRECIS_DATABASE_URL."
    )

    dp = psub.add_parser(
        "dump",
        help="Export one paper's full stored state as precis-paper-dump/1 JSON.",
        description=(
            "Write the same precis-paper-dump/1 block "
            "get(kind='paper', view='ops') serves, every slice merged "
            "into one JSON file — the undo artifact to take before a "
            "drop_body/move_body_to/split_body/clear_abstract run."
        ),
    )
    dp.add_argument(
        "id",
        help="Paper id: cite_key slug, paNNN handle, numeric ref_id or DOI.",
    )
    dp.add_argument(
        "--out",
        required=True,
        help="Destination file for the dump JSON.",
    )
    dp.add_argument(
        "--database-url", default=None, help="Override PRECIS_DATABASE_URL."
    )

    rs = psub.add_parser(
        "restore",
        help="Rebuild a paper from a precis-paper-dump/1 file (dry run default).",
        description=(
            "Feed one merged dump file (what `precis paper dump --out` "
            "writes) back through the slice-2 restore door as a single "
            "slice, in one transaction — a dry run unless --apply. The "
            "target defaults to the dump's own ref (same); 'new' mints a "
            "fresh ref under --handle or the dump's cite_key."
        ),
    )
    rs.add_argument(
        "file",
        help="The merged dump JSON `precis paper dump --out` wrote.",
    )
    rs.add_argument(
        "--apply",
        action="store_true",
        help="Actually write; the default is a dry run that writes nothing.",
    )
    rs.add_argument(
        "--target",
        choices=["same", "new"],
        default="same",
        help="Restore into the dump's own ref, or mint a fresh one.",
    )
    rs.add_argument(
        "--handle",
        default=None,
        help="Slug for a minted ref when the dump's cite_key is taken.",
    )
    rs.add_argument(
        "--reason",
        default="cli restore",
        help="Reason stamped on the ref_events (default: 'cli restore').",
    )
    rs.add_argument(
        "--database-url", default=None, help="Override PRECIS_DATABASE_URL."
    )


def run(args: argparse.Namespace) -> None:
    if args.paper_cmd == "authors-resplit":
        _run_authors_resplit(args)
        return
    if args.paper_cmd == "suspect-previews":
        _run_suspect_previews(args)
        return
    if args.paper_cmd == "fetch-si":
        _run_fetch_si(args)
        return
    if args.paper_cmd == "dump":
        _run_dump(args)
        return
    if args.paper_cmd == "restore":
        _run_restore(args)
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


def fetch_si(store: Store, id: str | int) -> tuple[str, str]:
    """Queue an SI fetch for one paper; returns ``(slug, status)``.

    ``status`` is ``'queued'`` or ``'pending'`` (a request is already
    waiting — nothing re-stamped). Raises :class:`NotFound` for an unknown
    id and :class:`BadInput` when the ref is itself a supplement (the
    error's ``next`` names the parent's fetch-si command). The lib call
    behind ``precis paper fetch-si``.
    """
    from precis.errors import BadInput
    from precis.handlers.paper import resolve_paper_slug
    from precis.store.si_links import request_si_fetch

    slug, ref_id = resolve_paper_slug(store, id)
    with store.pool.connection() as conn:
        status, parent = request_si_fetch(conn, ref_id, by="cli")
        conn.commit()
    if status == "is_supplement":
        assert parent is not None
        raise BadInput(
            f"{slug} is itself supplementary information of {parent[1] or parent[0]}",
            next=f"precis paper fetch-si {parent[1] or parent[0]}",
        )
    return slug, status


def dump_paper(store: Store, id: str | int) -> dict[str, Any]:
    """The lib behind ``precis paper dump``: every ``view='ops'`` slice
    merged into one ``precis-paper-dump/1`` dict. Iterates max-size
    windows rather than asking for ``size=0`` so the CLI exercises the
    same paging path the agent does."""
    from precis.handlers.paper import resolve_paper_slug
    from precis.ingest import paper_dump

    _slug, ref_id = resolve_paper_slug(store, id)
    with store.pool.connection() as conn:
        first = paper_dump.dump_ref(
            conn, ref_id, offset=0, size=paper_dump.MAX_PAGE_SIZE
        )
        chunks = list(first["chunks"])
        total = first["manifest"]["chunks_total"]
        while len(chunks) < total:
            nxt = paper_dump.dump_ref(
                conn, ref_id, offset=len(chunks), size=paper_dump.MAX_PAGE_SIZE
            )
            if not nxt["chunks"]:
                break
            chunks += nxt["chunks"]
        first["chunks"] = chunks
        first["manifest"]["window"] = [0, len(chunks)]
    return first


def _run_dump(args: argparse.Namespace) -> None:
    import json

    store = Store.connect(resolve_dsn(args.database_url))
    try:
        dump = dump_paper(store, args.id)
    except PrecisError as exc:
        print(_render_precis_error(exc), file=sys.stderr)
        sys.exit(REFUSAL_EXIT)
    finally:
        store.close()
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(dump, fh, indent=1)
        fh.write("\n")
    m = dump["manifest"]
    print(
        f"dumped {m['handle']}: {m['chunks_total']} chunk(s), "
        f"body_sha {m['body_sha'][:12]}… → {args.out}"
    )


def restore_paper(
    store: Store,
    dump: dict[str, Any],
    *,
    target: str = "same",
    handle: str | None = None,
    reason: str = "cli restore",
    dry: bool = True,
) -> Any:
    """The lib behind ``precis paper restore``: one merged dump file as
    a single ``restore_slice`` call in one transaction. On a real write
    the same two ref_events the ``put`` door records land here —
    ``paper_restore_slice`` always, ``paper_restored`` when the slice
    completes the body — stamped ``source='cli'``. Returns the
    :class:`paper_dump.RestoreReport` for the caller to render."""
    from precis.ingest import paper_dump

    with store.tx() as conn:
        report = paper_dump.restore_slice(
            conn, dump, target=target, handle=handle, dry=dry
        )
        if dry:
            return report
        store.append_event(
            report.ref_id,
            source="cli",
            event="paper_restore_slice",
            payload={
                "window": list(report.window),
                "inserted": report.inserted,
                "kept": report.kept,
                "replaced": report.replaced,
                "chunks_done": report.chunks_done,
                "chunks_total": report.chunks_total,
                "reason": reason,
            },
            conn=conn,
        )
        if report.completed:
            store.append_event(
                report.ref_id,
                source="cli",
                event="paper_restored",
                payload={
                    "dump_body_sha": (dump.get("manifest") or {}).get("body_sha"),
                    "chunks": report.chunks_total,
                    "cards": report.cards,
                    "identifiers": report.identifiers,
                    "links": report.links_written + report.links_kept,
                    "skipped_links": report.links_skipped,
                    "restamped": len(report.links_restamped),
                    "restamped_links": report.links_restamped,
                    "reason": reason,
                },
                conn=conn,
            )
    return report


def _run_restore(args: argparse.Namespace) -> None:
    import json

    from precis.handlers.paper import PaperHandler

    with open(args.file, encoding="utf-8") as fh:
        dump = json.load(fh)
    store = Store.connect(resolve_dsn(args.database_url))
    try:
        report = restore_paper(
            store,
            dump,
            target=args.target,
            handle=args.handle,
            reason=args.reason,
            dry=not args.apply,
        )
    except PrecisError as exc:
        print(_render_precis_error(exc), file=sys.stderr)
        sys.exit(REFUSAL_EXIT)
    finally:
        store.close()
    print(PaperHandler._render_restore_ack(report))
    if report.minted:
        print(f"minted {report.handle} under slug {report.slug!r}")
    if not args.apply:
        print("dry run — re-run with --apply to write")


def _render_precis_error(err: PrecisError) -> str:
    """The ``[error:Class] cause / next:`` envelope, as the verb surface
    renders it — kept local so this command doesn't need the runtime."""
    parts = [f"[error:{err.__class__.__name__}] {err.cause}"]
    if err.options:
        parts.append(f"  options: {', '.join(map(str, err.options))}")
    nexts = err.next if isinstance(err.next, list) else [err.next]
    parts += [f"  next: {h}" for h in nexts if h]
    return "\n".join(parts)


def _run_fetch_si(args: argparse.Namespace) -> None:
    store = Store.connect(resolve_dsn(args.database_url))
    try:
        slug, status = fetch_si(store, args.id)
    except PrecisError as exc:
        print(_render_precis_error(exc), file=sys.stderr)
        sys.exit(REFUSAL_EXIT)
    finally:
        store.close()
    state = "already queued" if status == "pending" else "queued"
    print(
        f"{state}: supplementary-information fetch for {slug} — the fetch "
        "worker's next pass discovers and downloads the SI; each PDF becomes "
        "its own linked supplement ref. The outcome lands on the paper "
        "(si_found / si_none / si_blocked / si_error)."
    )

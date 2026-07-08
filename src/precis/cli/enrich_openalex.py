"""``precis enrich-openalex`` — slurp free OpenAlex metadata onto paper refs.

The OpenAlex *work* object is free + keyless and richer than what we hold —
citations, topics, funders, ORCID+ROR affiliations. This writes it into
``meta.openalex`` (+ the ``openalex:W…`` id, + the byline when the ref has
none). Independent of the paid content pull (``precis fetch-openalex``).

Usage::

    precis enrich-openalex 10.3390/chemosensors11090486    # by DOI
    precis enrich-openalex 53423                           # by ref_id
    precis enrich-openalex --backfill --limit 200          # sweep un-enriched

Backfill claims paper refs that have a DOI but no (or a stale) ``meta.openalex``
block, newest first — a cheap, resumable sweep (re-run to continue). Network-
bound, so run it on-cluster.
"""

from __future__ import annotations

import argparse
import sys

from precis.cli._common import resolve_dsn
from precis.ingest.openalex_meta import ENRICH_VERSION, enrich_ref
from precis.store import Store


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "enrich-openalex",
        help="Slurp free OpenAlex metadata (citations/topics/ORCID) onto papers.",
        description=(
            "Enrich a paper ref (or backfill many) with the free OpenAlex work "
            "object: meta.openalex block, openalex id, byline when missing."
        ),
    )
    p.add_argument(
        "target",
        nargs="?",
        help="A DOI (10.xxxx/…) or a paper ref_id. Omit with --backfill.",
    )
    p.add_argument(
        "--backfill",
        action="store_true",
        help="Sweep paper refs with a DOI but no/stale meta.openalex block.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Max refs to enrich in --backfill mode (default 100).",
    )
    p.add_argument(
        "--email",
        default=None,
        help="Contact email for OpenAlex's polite pool "
        "(default PRECIS_UNPAYWALL_EMAIL).",
    )
    p.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )


def _doi_for_ref(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT min(id_value) FROM ref_identifiers "
            "WHERE ref_id = %s AND id_kind = 'doi'",
            (ref_id,),
        ).fetchone()
    return row[0] if row and row[0] else None


def _backfill_batch(store: Store, *, limit: int) -> list[tuple[int, str]]:
    sql = """
        SELECT r.ref_id,
               (SELECT min(id_value) FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'doi') AS doi
          FROM refs r
         WHERE r.kind = 'paper'
           AND r.deleted_at IS NULL
           AND EXISTS (SELECT 1 FROM ref_identifiers ri
                        WHERE ri.ref_id = r.ref_id AND ri.id_kind = 'doi')
           AND (
                 r.meta->'openalex' IS NULL
                 OR COALESCE((r.meta->'openalex'->>'v')::int, 0) < %s
           )
         ORDER BY r.ref_id DESC
         LIMIT %s
    """
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (ENRICH_VERSION, limit)).fetchall()
    return [(int(r[0]), r[1]) for r in rows if r[1]]


def _resolve_one(store: Store, target: str) -> tuple[int, str]:
    target = target.strip()
    if target.startswith("10.") and "/" in target:
        ref_id = store.find_paper_ref_by_identifier(target)
        if ref_id is None:
            raise SystemExit(
                f"enrich-openalex: no paper ref holds DOI {target} "
                "(nothing to enrich — ingest/stub it first)."
            )
        return ref_id, target
    if target.isdigit():
        doi = _doi_for_ref(store, int(target))
        if not doi:
            raise SystemExit(f"enrich-openalex: ref {target} has no DOI.")
        return int(target), doi
    raise SystemExit(
        f"enrich-openalex: '{target}' is neither a DOI nor a numeric ref_id."
    )


def run(args: argparse.Namespace) -> None:
    import os

    email = args.email or os.environ.get("PRECIS_UNPAYWALL_EMAIL", "").strip()
    store = Store.connect(resolve_dsn(args.database_url))
    try:
        if args.backfill:
            targets = _backfill_batch(store, limit=args.limit)
        else:
            if not args.target:
                raise SystemExit(
                    "enrich-openalex: pass a DOI / ref_id, or use --backfill."
                )
            targets = [_resolve_one(store, args.target)]

        if not targets:
            print("enrich-openalex: nothing to enrich.", file=sys.stderr)
            return

        enriched = missing = 0
        for ref_id, doi in targets:
            try:
                enr = enrich_ref(store, ref_id, doi=doi, email=email)
            except Exception as exc:
                print(
                    f"enrich-openalex: ref {ref_id} ({doi}) error: {exc}",
                    file=sys.stderr,
                )
                continue
            if enr is None:
                missing += 1
                continue
            enriched += 1
            n_refs = len(enr.meta.get("referenced_works", []))
            print(
                f"enrich-openalex: ref {ref_id} ← {enr.openalex_id} "
                f"({n_refs} refs, {len(enr.authorships)} authors)",
                file=sys.stderr,
            )
        print(
            f"enrich-openalex: {enriched} enriched, {missing} not-in-openalex, "
            f"of {len(targets)}.",
            file=sys.stderr,
        )
    finally:
        store.close()


__all__ = ["add_parser", "run"]

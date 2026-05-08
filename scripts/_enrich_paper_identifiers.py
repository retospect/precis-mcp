"""enrich-paper-identifiers — sweep all paper refs and populate
``ref_identifiers`` with the FULL Semantic Scholar externalIds
cluster (DOI / ArXiv / PubMed / PubMedCentralID / MAG / DBLP /
CorpusId / OpenAlex).

Why this exists: migration ``0009_ref_identifiers.sql`` backfilled
the four canonical aliases (DOI, arXiv id, S2 paperId, pdf_hash)
from existing ``refs.meta`` JSON. But papers ingested before
``acatome-meta`` started capturing the full S2 externalIds (April
2026 work) only ever had three of those identifiers persisted —
the wider cluster (PubMed, MAG, OpenAlex, ...) was thrown away by
``_normalize`` in ``acatome_meta/semantic_scholar.py``.

This sweep walks every live paper ref, picks the strongest S2
lookup key (DOI > arXiv > S2 paperId), queries S2 once per paper
to fetch ``externalIds``, and writes one ``ref_identifiers`` row
per cluster member (``source='s2'``). Idempotent via the open tag
``s2-enriched`` — the tag is added on completion so re-runs of
this sweep skip refs we've already enriched.

After this sweep, ``doilist scan`` sees the maximum-coverage alias
index — sources/ DOIs that match ANY known identifier of any
ingested paper get caught, not just the canonical four.

Usage:
    enrich-paper-identifiers                # walk every paper, sweep S2
    enrich-paper-identifiers --limit 100    # only first N refs (sanity check)
    enrich-paper-identifiers --re-enrich    # ignore the s2-enriched tag, re-query
    enrich-paper-identifiers --dry-run      # log what would happen, no writes

Env:
    PRECIS_DATABASE_URL              default postgresql://acatome:acatome@127.0.0.1:5432/precis
    SEMANTIC_SCHOLAR_API_KEY         optional, raises S2's free-tier rate limit
"""

from __future__ import annotations

import argparse
import os
import time
from typing import Any

# Soft delay between S2 calls. With API key, S2 allows ~10 req/s; without,
# ~1 req/s. We err on the polite side at 0.4s — gives us ~2.5 req/s steady
# state, well under the keyed limit, comfortably above the un-keyed.
INTER_CALL_SLEEP = 0.4

# S2 externalIds key -> our normalised scheme. Mirrors
# `precis.store._ingest_ops._S2_EXTERNAL_KEY_MAP`. Kept duplicated
# here rather than imported because this script is run via the
# wrapper outside the package import path.
_S2_KEY_MAP: dict[str, str] = {
    "DOI": "doi",
    "ArXiv": "arxiv",
    "PubMed": "pubmed",
    "PubMedCentralID": "pmc",
    "MAG": "mag",
    "DBLP": "dblp",
    "CorpusId": "corpusid",
    "OpenAlex": "openalex",
}

DEFAULT_DB = "postgresql://acatome:acatome@127.0.0.1:5432/precis"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _load_targets(
    db_url: str,
    *,
    re_enrich: bool,
    limit: int | None,
) -> list[dict[str, Any]]:
    """Return the list of paper refs that need an S2 round-trip.

    A ref is a target if:
      * It's a live paper (``kind='paper' AND deleted_at IS NULL``).
      * It has at least one S2-lookup-able key: DOI in
        ``ref_identifiers`` (scheme='doi'), arxiv id (scheme='arxiv'),
        or S2 paperId (scheme='s2'). Without one of these we have no
        way to ask S2 about the paper, so we skip it.
      * It does NOT carry the ``s2-enriched`` open tag (unless
        ``--re-enrich`` is set).

    Each row carries the strongest lookup key under ``s2_lookup``:
    DOI > arXiv > S2 paperId (matching how the ingest path resolves
    a paper).
    """
    import psycopg

    sql = """
        SELECT r.id           AS ref_id,
               r.slug         AS slug,
               r.title        AS title,
               (SELECT pi.value FROM ref_identifiers pi
                  WHERE pi.ref_id = r.id AND pi.scheme = 'doi'   LIMIT 1) AS doi,
               (SELECT pi.value FROM ref_identifiers pi
                  WHERE pi.ref_id = r.id AND pi.scheme = 'arxiv' LIMIT 1) AS arxiv_id,
               (SELECT pi.value FROM ref_identifiers pi
                  WHERE pi.ref_id = r.id AND pi.scheme = 's2'    LIMIT 1) AS s2_id,
               EXISTS (
                   SELECT 1 FROM ref_open_tags t
                   WHERE t.ref_id = r.id AND t.value = 's2-enriched'
               ) AS already_enriched
        FROM refs r
        WHERE r.kind = 'paper' AND r.deleted_at IS NULL
        ORDER BY r.id
    """
    out: list[dict[str, Any]] = []
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(sql)
        for row in cur.fetchall():
            ref_id, slug, title, doi, arxiv_id, s2_id, already_enriched = row
            if already_enriched and not re_enrich:
                continue
            # DOI > arXiv > S2 — strongest first because S2's DOI lookup
            # is the most reliable (CrossRef-side authority); arxiv id
            # is second (S2's preprint cluster is good); paperId direct
            # is last (only useful when we have no other key).
            if doi:
                lookup_key = doi
            elif arxiv_id:
                lookup_key = f"ARXIV:{arxiv_id}"
            elif s2_id:
                lookup_key = s2_id
            else:
                # No S2-lookup-able key. Skip — we have no way to
                # ask S2 about this paper.
                continue
            out.append(
                {
                    "ref_id": ref_id,
                    "slug": slug,
                    "title": title,
                    "doi": doi,
                    "arxiv_id": arxiv_id,
                    "s2_id": s2_id,
                    "lookup_key": lookup_key,
                }
            )
    if limit is not None:
        out = out[:limit]
    return out


def _build_s2_client(api_key: str) -> Any:
    """Construct the SemanticScholar client. Lazy import so help works."""
    from semanticscholar import SemanticScholar  # type: ignore[import-untyped]
    return SemanticScholar(api_key=api_key) if api_key else SemanticScholar()


def _build_retry_decorator() -> Any:
    """Same retry shape as `_find_citing_papers.py`. Lazy-imported."""
    import httpx
    from semanticscholar.SemanticScholarException import (  # type: ignore[import-untyped]
        GatewayTimeoutException,
        InternalServerErrorException,
        ServerErrorException,
    )
    from tenacity import (
        retry,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential,
    )

    transient = (
        ServerErrorException,
        InternalServerErrorException,
        GatewayTimeoutException,
        ConnectionRefusedError,
        httpx.ReadError,
        httpx.ConnectError,
        httpx.TimeoutException,
        httpx.RemoteProtocolError,
    )
    return retry(
        wait=wait_exponential(min=5, max=120),
        stop=stop_after_attempt(5),
        retry=retry_if_exception_type(transient),
        reraise=True,
    )


_DO_FETCH_WITH_RETRY: Any = None


def _fetch_external_ids(
    sch: Any,
    lookup_key: str,
) -> tuple[dict[str, str] | None, str | None, str | None]:
    """Fetch externalIds + paperId for one paper.

    Returns ``(external_ids, s2_paper_id, error)``. On terminal
    failure the dict is None and ``error`` carries a short message.
    """
    global _DO_FETCH_WITH_RETRY
    if _DO_FETCH_WITH_RETRY is None:
        _DO_FETCH_WITH_RETRY = _build_retry_decorator()(
            lambda sch, key: sch.get_paper(key, fields=["paperId", "externalIds"])
        )
    try:
        paper = _DO_FETCH_WITH_RETRY(sch, lookup_key)
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"[:200]
    if paper is None:
        return None, None, "S2 returned None (paper not found)"
    raw_external = getattr(paper, "externalIds", None) or {}
    out: dict[str, str] = {}
    if raw_external:
        for k, v in raw_external.items():
            if not k or v is None:
                continue
            sv = str(v).strip()
            if sv:
                out[str(k)] = sv
    s2_id = getattr(paper, "paperId", None)
    return out, s2_id, None


def _write_aliases(
    db_url: str,
    ref_id: int,
    external_ids: dict[str, str],
    s2_id: str | None,
    *,
    dry_run: bool,
) -> tuple[int, set[str]]:
    """INSERT new alias rows for ``ref_id``. Returns ``(rows_written, schemes_added)``."""
    import psycopg

    rows: list[tuple[str, str, int, str]] = []
    schemes: set[str] = set()
    if s2_id:
        sv = s2_id.strip().lower()
        if sv:
            rows.append(("s2", sv, ref_id, "s2"))
            schemes.add("s2")
    for raw_key, raw_val in external_ids.items():
        scheme = _S2_KEY_MAP.get(raw_key, raw_key.lower())
        v = raw_val.strip().lower()
        if not v:
            continue
        rows.append((scheme, v, ref_id, "s2"))
        schemes.add(scheme)
    if not rows:
        return 0, set()
    if dry_run:
        return len(rows), schemes
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO ref_identifiers (scheme, value, ref_id, source) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            rows,
        )
        return cur.rowcount or 0, schemes


def _mark_enriched(db_url: str, ref_id: int, *, dry_run: bool) -> None:
    """Add the ``s2-enriched`` open tag so re-runs skip this ref."""
    if dry_run:
        return
    import psycopg
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ref_open_tags (ref_id, pos, value, set_by) "
            "VALUES (%s, -1, 's2-enriched', 'system') "
            "ON CONFLICT DO NOTHING",
            (ref_id,),
        )


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only sweep the first N un-enriched refs (sanity-check before full run).",
    )
    p.add_argument(
        "--re-enrich",
        action="store_true",
        help="Ignore the `s2-enriched` open tag and re-query every paper.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would happen but write nothing to the DB.",
    )
    args = p.parse_args()

    db_url = os.environ.get("PRECIS_DATABASE_URL", DEFAULT_DB)
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
    if not api_key:
        log(
            "WARNING: SEMANTIC_SCHOLAR_API_KEY not set - using free tier "
            "(slow, prone to 429)."
        )

    log(f"loading targets from precis ({db_url}) ...")
    targets = _load_targets(db_url, re_enrich=args.re_enrich, limit=args.limit)
    if not targets:
        log("no refs need enrichment - nothing to do.")
        return
    log(f"  {len(targets)} paper(s) to enrich")

    sch = _build_s2_client(api_key)

    n_enriched = 0
    n_already_complete = 0  # S2 returned externalIds but all rows already existed
    n_failed = 0
    n_no_external_ids = 0  # S2 gave us nothing back
    schemes_seen: dict[str, int] = {}

    t0 = time.time()
    for i, t in enumerate(targets, start=1):
        ref_id = t["ref_id"]
        slug = t["slug"]
        lookup_key = t["lookup_key"]

        external_ids, s2_id, err = _fetch_external_ids(sch, lookup_key)
        if err is not None:
            n_failed += 1
            log(f"[{i}/{len(targets)}] {slug:40} FAIL  ({lookup_key}): {err}")
            time.sleep(INTER_CALL_SLEEP)
            continue
        # Even an empty external_ids dict counts as "we asked S2 and
        # got an answer" — mark the ref enriched so we don't re-ask.
        if not external_ids and not s2_id:
            n_no_external_ids += 1
            _mark_enriched(db_url, ref_id, dry_run=args.dry_run)
            log(f"[{i}/{len(targets)}] {slug:40} ok    (no externalIds returned)")
            time.sleep(INTER_CALL_SLEEP)
            continue

        rows_written, schemes = _write_aliases(
            db_url, ref_id, external_ids, s2_id, dry_run=args.dry_run
        )
        for s in schemes:
            schemes_seen[s] = schemes_seen.get(s, 0) + 1

        _mark_enriched(db_url, ref_id, dry_run=args.dry_run)

        if rows_written > 0:
            n_enriched += 1
            log(
                f"[{i}/{len(targets)}] {slug:40} ok    "
                f"(+{rows_written} rows, schemes={sorted(schemes)})"
            )
        else:
            n_already_complete += 1
            log(
                f"[{i}/{len(targets)}] {slug:40} ok    "
                f"(no new rows; cluster already in DB)"
            )

        time.sleep(INTER_CALL_SLEEP)

    elapsed = time.time() - t0
    log("")
    log("=" * 60)
    log(f"sweep done in {elapsed:.1f}s")
    log(f"  enriched (new rows):      {n_enriched}")
    log(f"  already-complete:         {n_already_complete}")
    log(f"  no externalIds returned:  {n_no_external_ids}")
    log(f"  failed:                   {n_failed}")
    if schemes_seen:
        log(f"  scheme tally:             {dict(sorted(schemes_seen.items()))}")
    if args.dry_run:
        log("  (dry-run: no DB writes)")


if __name__ == "__main__":
    main()

"""OpenAlex ``referenced_works`` → ``paper_bib_entries`` (gr513755 slice 2).

~8.7k live papers carry a reference-list chunk but zero
``paper_bib_entries`` rows — ``bib_parse`` resolves them via Crossref at
~3.6 papers/h (~100 days). The free OpenAlex work object already carries
the paper's whole reference list as ``referenced_works`` W-ids, in **one
call per paper**, through the same client / ``openalex`` limiter row /
``call_with_backoff`` politeness class as :mod:`precis.ingest.openalex_meta`.

For each candidate paper (live ``kind='paper'``, a ``references`` chunk,
no bib entries, a DOI or OpenAlex id — newest first):

1. Use ``meta.openalex.referenced_works`` when already cached (the
   ``openalex_enrich`` pass may have landed it); else fetch the work —
   OpenAlex id first, DOI second — and write the ``meta.openalex`` block
   + ``openalex`` identifier (the abstract is promoted top-level only
   when blank; byline is left to the enrich pass). A genuine 404 stamps
   ``meta.openalex = {tried_at, miss: true}`` so the paper converges.
2. Write one ``paper_bib_entries`` row per referenced work
   (``source='openalex'``, migration 0241): synthetic **negative**
   markers ``-1..-N`` (no real ``[N]`` exists; can't collide with
   bib_parse's positive markers), ``openalex_id`` = the W-id,
   ``match_conf=1.0`` (the OpenAlex edge is the identity — heldness is a
   separate question), ``parse_version=0`` — below
   ``BIB_PARSE_VERSION`` so ``_delete_stale_entries`` lets a later real
   parse *supersede* these stopgap rows (the minted ``cites`` links
   persist regardless).
3. Resolve each W-id to a held paper via ``ref_identifiers`` — the
   ``openalex`` kind first, ``doi`` second (a defensive fallback: the
   same value probed under both kinds costs one ``ANY`` clause and
   covers any w-id ever stored under the wrong kind). A held target's
   own DOI is copied onto the entry's ``doi`` column.
4. Mint paper-level ``cites`` links for the held targets via
   :func:`precis.workers.bib_parse.ensure_bib_cites_links`, stamped
   ``meta.source='oa_refs'``.

Idempotent: a paper with *any* ``paper_bib_entries`` rows is skipped —
so a re-run adds nothing and a Crossref-parsed paper is never touched.
``--limit`` bounds papers per run. A :class:`RateLimited` refusal from
the shared ``openalex`` ``external_rate_limits`` row (100k/day cap)
stops the run cleanly mid-list. Driven by ``precis backfill-oa-refs``;
the release owner runs it — nothing here is scheduled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.ingest import openalex_meta
from precis.utils.db_retry import retry_locked
from precis.utils.rate_limit import RateLimited

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: ``paper_bib_entries.source`` value for these rows (migration 0241's
#: CHECK enumerates it).
OA_REFS_SOURCE = "openalex"

#: ``links.meta.source`` for edges this path mints — distinct from
#: ``bib_parse``'s ``_LINK_SOURCE`` so provenance stays attributable.
OA_LINK_SOURCE = "oa_refs"

#: ``parse_version`` for OpenAlex-sourced rows: *no* parse ran, and 0 <
#: ``BIB_PARSE_VERSION`` means ``_delete_stale_entries`` removes them the
#: first time ``bib_parse`` really parses the paper — the parsed
#: bibliography supersedes this approximation without a dedup dance.
OA_REFS_PARSE_VERSION = 0

#: ``match_conf`` for OpenAlex-sourced rows: the referenced_works edge IS
#: the resolved identity (a W-id is authoritative, not a guess), so every
#: row is "matched" — ``held_ref_id`` says whether we hold the target.
_MATCH_CONF_OA = 1.0

#: ``ref_events.source`` / ``ref_identifiers.source`` for this writer.
_WRITE_SOURCE = "oa-refs"


@dataclass
class PaperOutcome:
    """One paper's result from :func:`materialize_oa_references`."""

    ref_id: int
    outcome: str  # written | skipped | miss | empty | no-identifier
    entries: int = 0
    held: int = 0
    links: int = 0
    fetched: bool = False


@dataclass
class OaRefsResult:
    """Aggregate of one :func:`backfill_oa_refs` run."""

    candidates: int = 0
    written: int = 0
    skipped: int = 0
    miss: int = 0
    empty: int = 0
    no_identifier: int = 0
    failed: int = 0
    entries: int = 0
    held: int = 0
    links: int = 0
    fetches: int = 0
    rate_limited: bool = False


def unparsed_candidates(store: Store, *, limit: int) -> list[int]:
    """ref_ids newest-first for the sweep: live papers with a
    ``references`` chunk and zero bib entries, fetchable (DOI or OpenAlex
    identifier) or already carrying a cached ``referenced_works`` list —
    minus papers whose stamp says done-forever (an OpenAlex miss, or a
    cached zero-length reference list).
    """
    sql = """
        SELECT r.ref_id
          FROM refs r
         WHERE r.kind = 'paper'
           AND r.retired_at IS NULL
           AND EXISTS (SELECT 1 FROM chunks c
                        WHERE c.ref_id = r.ref_id
                          AND c.chunk_kind = 'references'
                          AND c.retired_at IS NULL)
           AND NOT EXISTS (SELECT 1 FROM paper_bib_entries e
                            WHERE e.ref_id = r.ref_id)
           AND r.meta->'openalex'->>'miss' IS DISTINCT FROM 'true'
           AND COALESCE(NULLIF(r.meta->'openalex'->>'referenced_works_count',
                               '')::int, -1) <> 0
           AND (
                 EXISTS (SELECT 1 FROM ref_identifiers i
                          WHERE i.ref_id = r.ref_id
                            AND i.id_kind IN ('openalex', 'doi'))
                 OR r.meta->'openalex'->'referenced_works' IS NOT NULL
               )
         ORDER BY r.ref_id DESC
         LIMIT %s
    """
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
    return [int(r[0]) for r in rows]


def _paper_identifiers(conn: Any, ref_id: int) -> dict[str, str]:
    """``{openalex: w…, doi: …}`` identifier rows for one ref."""
    rows = conn.execute(
        "SELECT id_kind, id_value FROM ref_identifiers "
        "WHERE ref_id = %s AND id_kind IN ('openalex', 'doi')",
        (ref_id,),
    ).fetchall()
    return {str(k): str(v) for k, v in rows}


def _cached_referenced_works(meta: dict[str, Any] | None) -> list[str] | None:
    """``meta.openalex.referenced_works`` W-ids, or ``None`` when absent."""
    oa = (meta or {}).get("openalex")
    if not isinstance(oa, dict):
        return None
    refs = oa.get("referenced_works")
    if not isinstance(refs, list) or not refs:
        return None
    return [str(w) for w in refs if w]


def _stamp_miss(store: Store, ref_id: int) -> None:
    """Same "OpenAlex has no record" stamp as ``openalex_enrich`` writes —
    ``meta.openalex = {tried_at, miss: true}`` — so this paper is never
    re-fetched by either pass (a real 404, not a transient failure)."""
    store.update_paper_fields(
        ref_id,
        meta_patch={
            "openalex": {"tried_at": datetime.now(UTC).isoformat(), "miss": True}
        },
        source=_WRITE_SOURCE,
    )


def _fetch_work(
    store: Store, ref_id: int, ids: dict[str, str], *, email: str
) -> openalex_meta.OpenAlexEnrichment | None:
    """Fetch the paper's OpenAlex work — OpenAlex id first, DOI second —
    then write the ``meta.openalex`` block + ``openalex`` identifier.
    Returns the normalised enrichment, or ``None`` on a genuine 404 (the
    caller stamps the miss)."""
    oa_id = ids.get("openalex")
    doi = ids.get("doi")
    work = (
        openalex_meta.fetch_openalex_work_by_id(oa_id, email=email) if oa_id else None
    )
    if work is None and doi:
        work = openalex_meta.fetch_openalex_work(doi, email=email)
    if work is None:
        return None

    enr = openalex_meta.normalize(work)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    current_meta = (row[0] if row else None) or {}
    meta_patch: dict[str, Any] = {"openalex": enr.meta}
    if enr.meta.get("abstract") and not current_meta.get("abstract"):
        meta_patch["abstract"] = enr.meta["abstract"]
    store.update_paper_fields(ref_id, meta_patch=meta_patch, source=_WRITE_SOURCE)
    if enr.openalex_id:
        # insert_ref_identifiers (first-write-wins) deliberately, not
        # set_ref_identifier: in a bulk sweep a w-id claimed by a
        # different live ref is a duplicate-ref problem for elsewhere,
        # not a reason to fail this paper.
        store.insert_ref_identifiers(
            ref_id, [("openalex", enr.openalex_id, _WRITE_SOURCE)]
        )
    return enr


def _held_refs_for_oa_ids(conn: Any, w_ids: list[str]) -> dict[str, int]:
    """``{w_id (as passed, W… form): held ref_id}`` for the referenced
    works we hold — probed under ``openalex`` id_kind first, ``doi``
    second (see module docstring), against live papers only.
    """
    probes = [w.lower() for w in w_ids]
    rows = conn.execute(
        "SELECT DISTINCT ON (ri.id_value) ri.id_value, ri.ref_id "
        "FROM ref_identifiers ri "
        "JOIN refs r ON r.ref_id = ri.ref_id "
        "WHERE ri.id_kind IN ('openalex', 'doi') AND ri.id_value = ANY(%s) "
        "AND r.kind = 'paper' AND r.retired_at IS NULL "
        "ORDER BY ri.id_value, (ri.id_kind = 'openalex') DESC, ri.ref_id",
        (probes,),
    ).fetchall()
    by_lower = {str(v): int(rid) for v, rid in rows}
    return {w: by_lower[w.lower()] for w in w_ids if w.lower() in by_lower}


def _dois_for_refs(conn: Any, ref_ids: list[int]) -> dict[int, str]:
    """``{held ref_id: doi}`` — one ``doi`` identifier per held target,
    copied onto the resolved entry's ``doi`` column."""
    if not ref_ids:
        return {}
    rows = conn.execute(
        "SELECT ref_id, min(id_value) FROM ref_identifiers "
        "WHERE ref_id = ANY(%s) AND id_kind = 'doi' GROUP BY ref_id",
        (ref_ids,),
    ).fetchall()
    return {int(r): str(d) for r, d in rows}


def materialize_oa_references(
    store: Store, ref_id: int, *, email: str = ""
) -> PaperOutcome:
    """Fetch/cache + write + link one paper's OpenAlex reference list.

    Idempotent and safe to point at any paper: a paper that already has
    ``paper_bib_entries`` rows (from this pass *or* a real bib_parse)
    returns ``outcome='skipped'`` untouched. ``RateLimited`` propagates —
    the caller stops the run, not this paper.
    """
    from precis.workers.bib_parse import ensure_bib_cites_links

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM paper_bib_entries WHERE ref_id = %s), "
            "       r.meta "
            "FROM refs r WHERE r.ref_id = %s AND r.retired_at IS NULL",
            (ref_id, ref_id),
        ).fetchone()
        if row is None:
            return PaperOutcome(ref_id=ref_id, outcome="skipped")
        has_entries, meta = bool(row[0]), row[1]
        ids = _paper_identifiers(conn, ref_id)
        conn.commit()
    if has_entries:
        return PaperOutcome(ref_id=ref_id, outcome="skipped")

    w_ids = _cached_referenced_works(meta if isinstance(meta, dict) else None)
    fetched = False
    if w_ids is None:
        if not ids:
            return PaperOutcome(ref_id=ref_id, outcome="no-identifier")
        enr = _fetch_work(store, ref_id, ids, email=email)
        fetched = True
        if enr is None:
            try:
                _stamp_miss(store, ref_id)
            except Exception:
                log.exception("oa_refs: ref %d miss-stamp failed", ref_id)
            return PaperOutcome(ref_id=ref_id, outcome="miss", fetched=True)
        w_ids = _cached_referenced_works({"openalex": enr.meta})
        if not w_ids:
            return PaperOutcome(ref_id=ref_id, outcome="empty", fetched=True)

    held: dict[str, int] = {}
    dois: dict[int, str] = {}
    with store.pool.connection() as conn:
        held = _held_refs_for_oa_ids(conn, w_ids)
        dois = _dois_for_refs(conn, sorted(set(held.values())))
        conn.commit()

    def _write() -> tuple[int, int]:
        ensured = 0
        with store.pool.connection() as conn:
            pairs: list[tuple[int, int]] = []
            written = 0
            for i, w_id in enumerate(w_ids):
                held_ref_id = held.get(w_id)
                row = conn.execute(
                    "INSERT INTO paper_bib_entries "
                    "(ref_id, marker, raw_text, doi, held_ref_id, "
                    " match_conf, parse_version, source, openalex_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (ref_id, marker) DO NOTHING "
                    "RETURNING id",
                    (
                        ref_id,
                        -(i + 1),
                        f"openalex:{w_id}",
                        dois.get(held_ref_id) if held_ref_id else None,
                        held_ref_id,
                        _MATCH_CONF_OA,
                        OA_REFS_PARSE_VERSION,
                        OA_REFS_SOURCE,
                        w_id,
                    ),
                ).fetchone()
                if row is not None:
                    written += 1
                    if held_ref_id is not None:
                        pairs.append((int(row[0]), held_ref_id))
            ensured = ensure_bib_cites_links(
                store, ref_id, pairs, conn=conn, source=OA_LINK_SOURCE
            )
            conn.commit()
        return written, ensured

    written, links = retry_locked(_write, label=f"oa_refs write ref_id={ref_id}")
    return PaperOutcome(
        ref_id=ref_id,
        outcome="written",
        entries=written,
        held=len(held),
        links=links,
        fetched=fetched,
    )


def backfill_oa_refs(store: Store, *, limit: int, email: str = "") -> OaRefsResult:
    """Run the sweep over at most ``limit`` candidate papers.

    One OpenAlex call per paper (zero when ``meta.openalex`` already
    caches the list). Stops cleanly on the first ``RateLimited`` — the
    shared ``openalex`` limiter row enforces the 100k/day cap; unworked
    candidates simply re-select next run.
    """
    out = OaRefsResult()
    for ref_id in unparsed_candidates(store, limit=limit):
        out.candidates += 1
        try:
            r = materialize_oa_references(store, ref_id, email=email)
        except RateLimited:
            log.info(
                "oa_refs: shared openalex limiter refused at ref %d; stopping the run",
                ref_id,
            )
            out.rate_limited = True
            break
        except Exception:
            log.exception("oa_refs: ref %d failed", ref_id)
            out.failed += 1
            continue
        out.fetches += int(r.fetched)
        out.entries += r.entries
        out.held += r.held
        out.links += r.links
        if r.outcome == "written":
            out.written += 1
        elif r.outcome == "skipped":
            out.skipped += 1
        elif r.outcome == "miss":
            out.miss += 1
        elif r.outcome == "empty":
            out.empty += 1
        elif r.outcome == "no-identifier":
            out.no_identifier += 1
    return out


__all__ = [
    "OA_LINK_SOURCE",
    "OA_REFS_PARSE_VERSION",
    "OA_REFS_SOURCE",
    "OaRefsResult",
    "PaperOutcome",
    "backfill_oa_refs",
    "materialize_oa_references",
    "unparsed_candidates",
]

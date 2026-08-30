"""Paper metadata enrichment pass — re-resolve authors/entry_type/journal/
identifiers/retraction status from Crossref (+OpenAlex) on a cadence.

Legacy imports left ``refs.authors`` as flat, mixed-format ``{"name"}``
strings and dropped Crossref/OpenAlex signal (document type, ISSN,
retraction notices, per-author ORCID, the PubMed/OpenAlex id cluster) on
the floor. ``precis.ingest.paper_meta_enrich.enrich_paper`` is the
per-ref choke point (one Crossref fetch + one conditional OpenAlex fetch);
this module claims a batch of unvisited paper refs and calls it,
draining the whole corpus over many passes.

Same two guards as ``paper_reconcile`` / ``openalex_enrich`` (see those
modules' docstrings for the detailed rationale):

* **Cadence throttle.** A ``paper_meta_enrich:last_run`` marker in
  ``app_state`` gates the whole pass to once per
  ``PRECIS_PAPER_META_ENRICH_REFRESH_HOURS`` (default 6).
* **Single-runner advisory lock.** A **transaction-scoped**
  ``pg_try_advisory_xact_lock`` (a distinct key, namespaced away from the
  sibling passes') held on one dedicated connection for the whole pass,
  so only one cluster node runs it per throttle window even under
  pgbouncer ``pool_mode=transaction``.

Idempotent: candidate selection is ``meta->>'authors_resolved_at' IS
NULL`` — ``enrich_paper`` stamps that key on every ref it visits
(hit, miss, or error), so a second pass over the same rows selects zero
candidates and is a no-op.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import psycopg

from precis import settings
from precis.store import Store
from precis.workers import _throttle
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)

#: Fixed signed-bigint key for the single-runner advisory lock. Arbitrary
#: constant, namespaced away from paper_reconcile's / openalex_enrich's.
_LOCK_KEY = 0x70_6D_65_74_61_65_6E_72 - 2**63  # "pmetaenr", mapped signed
#: app_state key holding the ISO-8601 timestamp of the last completed pass.
_STATE_KEY = "paper_meta_enrich:last_run"
#: Env var + default for the cadence throttle (see :func:`_throttle.due`).
_REFRESH_ENV_VAR = "PRECIS_PAPER_META_ENRICH_REFRESH_HOURS"
_DEFAULT_REFRESH_HOURS = 6.0

#: Batch size when the caller doesn't pass a ``limit``.
_DEFAULT_BATCH_LIMIT = 50


def _due(store: Store) -> bool:
    """True when the throttle window has elapsed since the last pass."""
    return _throttle.due(store, _STATE_KEY, _REFRESH_ENV_VAR, _DEFAULT_REFRESH_HOURS)


def _claim_batch(store: Store, *, limit: int) -> list[tuple[int, str | None]]:
    """Unvisited paper refs, newest first: ``(ref_id, doi_or_none)``.

    A DOI-less ref is included (``doi`` is ``None``) — ``enrich_paper``
    degrades to the no-network heuristic author split for those.
    """
    sql = """
        SELECT r.ref_id,
               (SELECT min(id_value) FROM ref_identifiers ri
                 WHERE ri.ref_id = r.ref_id AND ri.id_kind = 'doi') AS doi
          FROM refs r
         WHERE r.kind = 'paper'
           AND r.deleted_at IS NULL
           AND r.meta->>'authors_resolved_at' IS NULL
         ORDER BY r.ref_id DESC
         LIMIT %s
    """
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
    return [(int(r[0]), r[1]) for r in rows]


def run_paper_meta_enrich_pass(
    store: Store, *, limit: int | None = None
) -> BatchResult:
    """Run the enrichment sweep if due; otherwise no-op.

    ``claimed`` counts refs selected this pass; ``ok`` counts refs
    ``enrich_paper`` returned a result for (visited, regardless of
    hit/miss); ``failed`` counts refs whose enrichment raised (logged,
    row left unvisited so it's retried next pass). Idle passes (no dsn,
    throttled, or lock-contended) return all zeros.
    """
    idle = BatchResult(handler="paper_meta_enrich", claimed=0, ok=0, failed=0)
    if not store.dsn or not _due(store):
        return idle
    dsn = store.dsn

    from precis.ingest.paper_meta_enrich import enrich_paper

    mailto = (settings.get_str("contact.crossref_mailto") or "").strip()
    email = (settings.get_str("contact.polite_email") or "").strip()
    batch_limit = limit if limit is not None else _DEFAULT_BATCH_LIMIT

    # Single-runner lock: same rationale as paper_reconcile / openalex_enrich
    # (see those modules' docstrings) — held on a dedicated connection,
    # transaction-scoped, for the whole pass.
    conn = psycopg.connect(dsn)
    try:
        with conn.transaction():
            row = conn.execute(
                "SELECT pg_try_advisory_xact_lock(%s)", (_LOCK_KEY,)
            ).fetchone()
            if not (row and row[0]):
                return idle  # another node owns the sweep this cycle

            batch = _claim_batch(store, limit=batch_limit)
            ok = 0
            failed = 0
            for ref_id, doi in batch:
                try:
                    outcome = enrich_paper(
                        store, ref_id, doi=doi, mailto=mailto, email=email
                    )
                except Exception:
                    log.exception(
                        "paper_meta_enrich: ref %d (%s) enrich failed", ref_id, doi
                    )
                    failed += 1
                    continue
                if outcome is not None:
                    ok += 1

            store.set_setting(_STATE_KEY, datetime.now(UTC).isoformat())

            if batch:
                log.info(
                    "paper_meta_enrich: visited %d ref(s), %d ok, %d failed",
                    len(batch),
                    ok,
                    failed,
                )
            return BatchResult(
                handler="paper_meta_enrich", claimed=len(batch), ok=ok, failed=failed
            )
    finally:
        conn.close()


__all__ = ["run_paper_meta_enrich_pass"]

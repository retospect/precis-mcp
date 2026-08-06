"""OpenAlex abstract/metadata enrich pass — self-healing on a cadence.

``precis enrich-openalex`` has always been a manual CLI sweep; nothing
scheduled ran it, so top-level abstracts (what the paper page reads —
``refs.meta['abstract']``) stayed empty even when the free OpenAlex
reconstruction was one keyless fetch away. This wires the same enrichment
into the system worker at a low cadence, in two lanes:

* **Lane A — promote (no network).** Any paper ref that already has a
  ``meta.openalex.abstract`` (fetched by a prior pass, or by the manual
  CLI) but no top-level ``meta.abstract`` gets it copied up in one
  set-based ``UPDATE``. Cheap, runs every due pass.
* **Lane B — fetch (network, drip).** A small batch of DOI'd paper refs
  with no ``meta.openalex`` block yet are enriched via
  :func:`precis.ingest.openalex_meta.enrich_ref`, which now also performs
  the Lane A promotion for the ref it just fetched.

Same two guards as ``paper_reconcile`` (see that module's docstring for the
detailed rationale):

* **Cadence throttle.** An ``openalex_enrich:last_run`` marker in
  ``app_state`` gates the whole pass to once per
  ``PRECIS_OPENALEX_ENRICH_REFRESH_HOURS`` (default 6).
* **Single-runner advisory lock.** A **transaction-scoped**
  ``pg_try_advisory_xact_lock`` (a distinct key, namespaced away from
  ``paper_reconcile``'s) held on one dedicated connection for the whole
  pass, so only one cluster node runs it per throttle window even under
  pgbouncer ``pool_mode=transaction``.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

import psycopg

from precis.store import Store
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)

#: Fixed signed-bigint key for the single-runner advisory lock. Arbitrary
#: constant, namespaced away from paper_reconcile's lock key.
_LOCK_KEY = 0x6F_61_65_6E_72_69_63_68 - 2**63  # "oaenrich", mapped signed
#: app_state key holding the ISO-8601 timestamp of the last completed pass.
_STATE_KEY = "openalex_enrich:last_run"

#: Lane B batch size when the caller doesn't pass a ``limit``.
_DEFAULT_FETCH_LIMIT = 50


def _refresh_hours() -> float:
    """Minimum gap between passes.

    ``PRECIS_OPENALEX_ENRICH_REFRESH_HOURS`` (default 6.0, floor 0.1).
    """
    raw = os.environ.get("PRECIS_OPENALEX_ENRICH_REFRESH_HOURS")
    if not raw:
        return 6.0
    try:
        return max(0.1, float(raw))
    except ValueError:
        return 6.0


def _due(store: Store) -> bool:
    """True when the throttle window has elapsed since the last pass."""
    last = store.get_setting(_STATE_KEY)
    if not last:
        return True
    try:
        last_ts = datetime.fromisoformat(last)
    except ValueError:
        return True
    return datetime.now(UTC) - last_ts >= timedelta(hours=_refresh_hours())


def _promote_openalex_abstracts(store: Store, *, limit: int | None) -> int:
    """Lane A: copy ``meta.openalex.abstract`` up to top-level ``meta.abstract``.

    Set-based, no network. Only touches refs where the top-level abstract is
    absent/blank and an OpenAlex-reconstructed abstract is on hand.
    """
    sql = """
        UPDATE refs
           SET meta = jsonb_set(meta, '{abstract}', meta->'openalex'->'abstract'),
               updated_at = now()
         WHERE ref_id IN (
             SELECT ref_id FROM refs
              WHERE kind = 'paper'
                AND deleted_at IS NULL
                AND COALESCE(meta->>'abstract', '') = ''
                AND COALESCE(meta->'openalex'->>'abstract', '') != ''
              ORDER BY ref_id DESC
              LIMIT %s
         )
    """
    with store.pool.connection() as conn:
        cur = conn.execute(sql, (limit or _DEFAULT_FETCH_LIMIT * 10,))
        return max(cur.rowcount, 0)


def _fetch_batch(store: Store, *, limit: int) -> list[tuple[int, str]]:
    """Lane B candidates: DOI'd paper refs with no ``meta.openalex`` block yet."""
    sql = """
        SELECT r.ref_id,
               (SELECT min(id_value) FROM ref_identifiers ri
                 WHERE ri.ref_id = r.ref_id AND ri.id_kind = 'doi') AS doi
          FROM refs r
         WHERE r.kind = 'paper'
           AND r.deleted_at IS NULL
           AND COALESCE(r.meta->>'abstract', '') = ''
           AND r.meta->'openalex' IS NULL
           AND EXISTS (SELECT 1 FROM ref_identifiers ri
                        WHERE ri.ref_id = r.ref_id AND ri.id_kind = 'doi')
         ORDER BY r.ref_id DESC
         LIMIT %s
    """
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
    return [(int(r[0]), r[1]) for r in rows if r[1]]


def run_openalex_enrich_pass(store: Store, *, limit: int | None = None) -> BatchResult:
    """Run the OpenAlex abstract-fill sweep if due; otherwise no-op.

    ``claimed``/``ok`` count Lane A promotions + Lane B fetch successes;
    ``failed`` stays 0 (a fetch that raises is logged and just doesn't
    count, mirroring ``paper_reconcile``). Idle passes (no dsn, throttled,
    or lock-contended) return all zeros.
    """
    idle = BatchResult(handler="openalex_enrich", claimed=0, ok=0, failed=0)
    if not store.dsn or not _due(store):
        return idle
    dsn = store.dsn

    from precis.ingest.openalex_meta import enrich_ref

    email = os.environ.get("PRECIS_UNPAYWALL_EMAIL", "").strip()
    fetch_limit = limit if limit is not None else _DEFAULT_FETCH_LIMIT

    # Single-runner lock: same rationale as paper_reconcile (see module
    # docstring) — held on a dedicated connection, transaction-scoped, for
    # the whole pass.
    conn = psycopg.connect(dsn)
    try:
        with conn.transaction():
            row = conn.execute(
                "SELECT pg_try_advisory_xact_lock(%s)", (_LOCK_KEY,)
            ).fetchone()
            if not (row and row[0]):
                return idle  # another node owns the sweep this cycle

            promoted = _promote_openalex_abstracts(store, limit=limit)

            fetched = 0
            for ref_id, doi in _fetch_batch(store, limit=fetch_limit):
                try:
                    enr = enrich_ref(store, ref_id, doi=doi, email=email)
                except Exception:
                    log.exception(
                        "openalex_enrich: ref %d (%s) fetch failed", ref_id, doi
                    )
                    continue
                if enr is not None:
                    fetched += 1

            store.set_setting(_STATE_KEY, datetime.now(UTC).isoformat())

            if promoted or fetched:
                log.info(
                    "openalex_enrich: promoted %d abstract(s), fetched %d new "
                    "OpenAlex record(s)",
                    promoted,
                    fetched,
                )
            work = promoted + fetched
            # NOTE: promoting/filling meta.abstract does not rebuild the
            # card_abstract search card — that touches the embedding
            # cascade and is a deliberate follow-on, not done here.
            return BatchResult(
                handler="openalex_enrich", claimed=work, ok=work, failed=0
            )
    finally:
        conn.close()


__all__ = ["run_openalex_enrich_pass"]

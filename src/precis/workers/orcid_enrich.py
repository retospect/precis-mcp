"""ORCID identity tier — background worker + cross-check
(precis.utils.authors module docstring).

12,008 ``kind='orcid'`` nodes exist in prod, almost all as name+iD stubs
minted by :mod:`precis.ingest.paper_meta_enrich` (a Crossref/OpenAlex
per-author ORCID hits a stub, but ``get(kind='orcid', ...)`` fleshing it
out — names, bio, employments, the full works list — was previously
on-demand only, gated behind an LLM deciding to resolve it. This module
is the gentle background sweep that drains that backlog on its own:

* **Fetch.** Claims a batch of unvisited ``kind='orcid'`` nodes
  (``meta.fetched_at IS NULL``), newest-linked-paper first, calls
  :func:`precis.ingest.orcid.fetch_record` for each, and stores the
  record via :func:`precis.handlers.orcid.store_orcid_record` — the same
  storage path ``OrcidHandler.get()`` uses, so a background-fetched node
  renders identically to a hand-resolved one. Held works are linked
  (``enqueue_authored_works(..., limit=0)`` — no stub minting from a
  background pass, mirroring the handler's plain-resolve default).
* **Cross-check.** For every ``authored`` edge of the node (a paper it's
  now linked to, old or new), tests the paper's DOI against the fresh
  record's works DOIs. A match stamps the matching ``paper_authors`` row
  (matched by ``orcid``) ``verified_at`` and — unless that row already
  carries ``source='human'`` — overwrites its names from the ORCID
  record and sets ``source='orcid'``. No match leaves the row untouched
  and stamps the edge ``meta.orcid_unconfirmed = true`` instead.

Same two guards as ``paper_reconcile``/``openalex_enrich`` (see those
modules' docstrings for the detailed rationale):

* **Cadence throttle.** An ``orcid_enrich:last_run`` marker in
  ``app_state`` gates the whole pass to once per
  ``PRECIS_ORCID_ENRICH_REFRESH_HOURS`` (default 1).
* Unlike those two, this pass carries no single-runner advisory lock —
  its claim predicate (``meta.fetched_at IS NULL``) is naturally
  converging (a node visited by any node never re-selects), so a rare
  double-claim across two racing cluster nodes just repeats one fetch,
  never corrupts anything.

**Credentials.** ``ORCID_CLIENT_ID``/``ORCID_CLIENT_SECRET`` missing
(:func:`precis.ingest.orcid.has_credentials`) raises one ``kind='alert'``
per process (never a silent idle pass — the vault-OAuth outage lesson:
an unauthenticated worker must be loud) and claims nothing. A
module-level flag suppresses every later call in the same process so a
long-idle worker doesn't re-raise (and re-round-trip the DB) every due
pass while the secret stays unset.

**Rate.** Fetches inside one batch are paced to at most
``_MAX_FETCHES_PER_SECOND`` (2) via a ``time.sleep``-based helper,
injectable as ``sleep_fn=`` for tests.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from precis import alerts
from precis.handlers.orcid import enqueue_authored_works, store_orcid_record
from precis.identity import normalize_doi
from precis.ingest import orcid as orcid_api
from precis.store import Store
from precis.utils.authors import split_middle
from precis.utils.env import env_int
from precis.workers import _throttle
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)

#: Env var + default for the cadence throttle (see :func:`_throttle.due`).
_REFRESH_ENV_VAR = "PRECIS_ORCID_ENRICH_REFRESH_HOURS"
_DEFAULT_REFRESH_HOURS = 1.0
#: app_state key holding the ISO-8601 timestamp of the last completed pass.
_STATE_KEY = "orcid_enrich:last_run"

#: Batch size when the caller doesn't pass a ``limit``.
_DEFAULT_BATCH_LIMIT = 100
_BATCH_ENV_VAR = "PRECIS_ORCID_ENRICH_BATCH"

#: Retry backoff for a node whose fetch failed: hold it back
#: ``min(fail_count, _MAX_BACKOFF_STEPS) * _BACKOFF_STEP_HOURS`` before
#: trying again (6h, 12h, … capped at 30h).
#:
#: Without this the pass had no failure memory at all — a node that fails
#: never gets ``meta.fetched_at``, so the claim query re-picked it every
#: pass forever. Measured on prod 2026-09-26: 18 nodes failing 28× each in
#: 24h = 501 of the fleet's 516 ERROR rows (97%), and 18 of every 100-node
#: batch spent on rows already known to fail, against a 93k-node claimable
#: backlog. A permanently-dead iD (deleted/withdrawn record) still costs
#: one attempt per capped window instead of one per pass.
_BACKOFF_STEP_HOURS = 6
_MAX_BACKOFF_STEPS = 5

#: Gentle-by-decision pacing (precis.utils.authors module docstring): the
#: ORCID public API allows far more, but this tier deliberately trickles.
_MAX_FETCHES_PER_SECOND = 2.0
_MIN_INTERVAL_S = 1.0 / _MAX_FETCHES_PER_SECOND

#: Set True after the missing-credentials alert has fired once this
#: process — guards against re-raising (and re-round-tripping the DB via
#: ``alerts.raise_alert``) every due pass while ORCID_CLIENT_ID/_SECRET
#: stay unset. Tests reset it directly
#: (``orcid_enrich._CREDENTIALS_ALERT_RAISED = False``).
_CREDENTIALS_ALERT_RAISED = False


def _due(store: Store) -> bool:
    """True when the throttle window has elapsed since the last pass."""
    return _throttle.due(store, _STATE_KEY, _REFRESH_ENV_VAR, _DEFAULT_REFRESH_HOURS)


def _batch_limit() -> int:
    return env_int(_BATCH_ENV_VAR, _DEFAULT_BATCH_LIMIT, lo=1)


def _raise_missing_credentials_alert(store: Store) -> None:
    """Fire the loud one-shot alert for missing ORCID client credentials."""
    global _CREDENTIALS_ALERT_RAISED
    if _CREDENTIALS_ALERT_RAISED:
        return
    alerts.raise_alert(
        store,
        source="orcid_enrich",
        fingerprint="orcid_enrich:missing_credentials",
        title="orcid_enrich: ORCID client credentials not configured",
        detail=(
            "ORCID_CLIENT_ID / ORCID_CLIENT_SECRET are not set — the "
            "background ORCID identity tier cannot fetch author records "
            "and is idling every due pass."
        ),
        severity="warn",
    )
    _CREDENTIALS_ALERT_RAISED = True


def _claim_batch(store: Store, *, limit: int) -> list[int]:
    """Unvisited ORCID node ref_ids, newest-linked-paper first.

    "Newest" is the largest ``ref_id`` among the node's ``authored``
    (out) edges' paper targets — a fresh stub (just minted alongside a
    just-fetched paper) floats to the front; a node with no linked paper
    yet (``NULLS LAST``) sorts behind every node that has one.
    """
    sql = """
        SELECT o.ref_id
          FROM refs o
         WHERE o.kind = 'orcid'
           AND o.retired_at IS NULL
           AND o.meta->>'fetched_at' IS NULL
           -- a node with no iD can never be fetched: leave it out rather
           -- than re-claim + fail it every pass
           AND (coalesce(o.meta->>'orcid_id', '') <> ''
                OR EXISTS (SELECT 1 FROM ref_identifiers ri
                            WHERE ri.ref_id = o.ref_id
                              AND ri.id_kind = 'cite_key'
                              AND ri.id_value LIKE 'orcid:%%'))
           -- ...and the same reasoning for one that HAS failed: hold it
           -- back for a widening window instead of re-failing it every
           -- pass (see _BACKOFF_STEP_HOURS / _record_failure).
           AND (o.meta->>'fetch_failed_at' IS NULL
                OR (o.meta->>'fetch_failed_at')::timestamptz
                   < now() - (least(
                         greatest(coalesce((o.meta->>'fetch_fail_count')::int, 1), 1),
                         %s
                     ) * %s * interval '1 hour'))
         ORDER BY (
             SELECT max(l.dst_ref_id) FROM links l
              WHERE l.src_ref_id = o.ref_id AND l.relation = 'authored'
         ) DESC NULLS LAST, o.ref_id DESC
         LIMIT %s
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            sql, (_MAX_BACKOFF_STEPS, _BACKOFF_STEP_HOURS, limit)
        ).fetchall()
    return [int(r[0]) for r in rows]


def _record_failure(store: Store, ref_id: int) -> None:
    """Stamp this node's failure so :func:`_claim_batch` holds it back.

    ``meta.fetch_failed_at`` is the last attempt; ``meta.fetch_fail_count``
    counts consecutive failures and widens the backoff. A success needs no
    counter reset — ``store_orcid_record`` sets ``meta.fetched_at``, which
    drops the node out of the claim query for good.

    Best-effort: a failed stamp must not abort the batch (the node simply
    stays claimable, i.e. today's behaviour), so this never raises.
    """
    ref = store.fetch_refs_by_ids([ref_id]).get(ref_id)
    prior = 0
    if ref is not None:
        try:
            prior = int((ref.meta or {}).get("fetch_fail_count") or 0)
        except (TypeError, ValueError):
            prior = 0
    try:
        store.stamp_ref_meta(
            ref_id,
            {
                "fetch_failed_at": datetime.now(UTC).isoformat(),
                "fetch_fail_count": prior + 1,
            },
        )
    except Exception:
        log.exception("orcid_enrich: could not stamp failure on node %d", ref_id)


def _orcid_id_for_node(store: Store, ref_id: int) -> str | None:
    """This node's canonical dashed ORCID iD, from ``meta.orcid_id`` (set
    on every node, stub or fetched — see
    ``ingest.paper_meta_enrich._mint_and_link_orcid_authors``) with a
    slug-parse fallback for a hand-inserted row missing it."""
    ref = store.fetch_refs_by_ids([ref_id]).get(ref_id)
    if ref is None:
        return None
    meta = ref.meta or {}
    orcid_id = meta.get("orcid_id")
    if isinstance(orcid_id, str) and orcid_id.strip():
        return orcid_id.strip()
    slug = ref.slug or ""
    if slug.startswith("orcid:"):
        try:
            return orcid_api.normalize_orcid_id(slug)
        except Exception:
            return None
    return None


def _cross_check_node(
    store: Store,
    node_ref_id: int,
    orcid_id: str,
    record: dict[str, Any],
) -> None:
    """Verify each ``authored`` edge of *node_ref_id* against *record*'s
    works DOIs, updating the matching ``paper_authors`` row (or flagging
    the edge unconfirmed) per precis.utils.authors module docstring."""
    works = record.get("works") or []
    work_dois = {normalize_doi(w.get("doi")) for w in works if w.get("doi")}
    work_dois.discard(None)

    edges = store.links_for(node_ref_id, direction="out", relation="authored")
    if not edges:
        return
    paper_ref_ids = [e.dst_ref_id for e in edges]
    dois_by_ref = store.dois_for_refs(paper_ref_ids)

    record_given = record.get("given") or ""
    record_family = record.get("family") or ""
    given, middle = split_middle(record_given)

    for edge in edges:
        paper_ref_id = edge.dst_ref_id
        doi = normalize_doi(dois_by_ref.get(paper_ref_id))
        matched = doi is not None and doi in work_dois
        store.add_link(
            src_ref_id=node_ref_id,
            dst_ref_id=paper_ref_id,
            relation="authored",
            set_by=edge.set_by,
            meta={"verified": True} if matched else {"orcid_unconfirmed": True},
            merge_meta=True,
        )
        if not matched:
            continue
        for row in store.get_paper_authors(paper_ref_id):
            if row.get("orcid") != orcid_id:
                continue
            if row.get("source") == "human":
                store.verify_paper_author(paper_ref_id, row["position"])
            else:
                store.verify_paper_author(
                    paper_ref_id,
                    row["position"],
                    given=given,
                    middle=middle,
                    family=record_family,
                    source="orcid",
                )


def run_once(
    store: Store,
    *,
    limit: int | None = None,
    fetch_fn: Callable[[str], dict[str, Any]] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> BatchResult:
    """Run the ORCID fetch + cross-check sweep if due; otherwise no-op.

    ``claimed`` counts nodes selected this pass; ``ok`` counts nodes
    fetched + stored + cross-checked; ``failed`` counts nodes whose fetch
    raised — logged, ``meta.fetched_at`` left unset, and the node stamped
    (:func:`_record_failure`) so :func:`_claim_batch` holds it back for a
    widening window rather than re-failing it on every pass. Idle passes
    (no dsn, throttled, or missing credentials) return all zeros and claim
    nothing.
    """
    idle = BatchResult(handler="orcid_enrich", claimed=0, ok=0, failed=0)
    if not store.dsn or not _due(store):
        return idle

    if not orcid_api.has_credentials():
        _raise_missing_credentials_alert(store)
        return idle

    fetch = fetch_fn or orcid_api.fetch_record
    batch_limit = limit if limit is not None else _batch_limit()
    node_ids = _claim_batch(store, limit=batch_limit)

    ok = 0
    failed = 0
    for ref_id in node_ids:
        orcid_id = _orcid_id_for_node(store, ref_id)
        if orcid_id is None:
            log.warning("orcid_enrich: node %d has no resolvable orcid_id", ref_id)
            _record_failure(store, ref_id)
            failed += 1
            continue
        try:
            record = fetch(orcid_id)
        except Exception:
            log.exception("orcid_enrich: node %d (%s) fetch failed", ref_id, orcid_id)
            _record_failure(store, ref_id)
            failed += 1
            continue
        finally:
            sleep_fn(_MIN_INTERVAL_S)

        store_orcid_record(store, record, existing_ref_id=ref_id)
        enqueue_authored_works(store, ref_id, record.get("works") or [], limit=0)
        _cross_check_node(store, ref_id, orcid_id, record)
        ok += 1

    store.set_setting(_STATE_KEY, datetime.now(UTC).isoformat())

    if node_ids:
        log.info(
            "orcid_enrich: visited %d node(s), %d ok, %d failed",
            len(node_ids),
            ok,
            failed,
        )
    return BatchResult(
        handler="orcid_enrich", claimed=len(node_ids), ok=ok, failed=failed
    )


__all__ = ["run_once"]

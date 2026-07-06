"""Paper-dedup reconcile pass — run the duplicate reconcilers on a cadence.

The ``precis reconcile-duplicates`` CLI has always been **manual only**;
nothing scheduled ran it, so duplicate paper refs accumulated until an
operator remembered to sweep. This pass wires the same reconcilers into
the system worker at a low cadence so the corpus self-heals:

* :func:`reconcile_by_title_similarity` — the motivating case: an id-less
  title-only stub minted for a paper we already hold (no shared
  identifier to collapse on). Auto-merges the high-confidence band only;
  the ambiguous band is logged, never merged.
* :func:`reconcile_by_pdf_sha256` / :func:`reconcile_by_doi_case` — the
  identifier/file duplicate classes. Idempotent and cheap once the corpus
  is clean.

Two guards keep it from being expensive or racy:

* **Cadence throttle.** A ``paper_reconcile:last_run`` marker in
  ``app_state`` gates the whole pass to once per
  ``PRECIS_PAPER_RECONCILE_REFRESH_HOURS`` (default 24). Between runs the
  pass is a single cheap ``app_state`` read, so the per-minute worker loop
  doesn't re-scan the corpus.
* **Single-runner advisory lock.** The reconcile writes corpus-wide (not
  node-local like ``corpus_reconcile``), so a **transaction-scoped**
  ``pg_try_advisory_xact_lock`` — held inside one open transaction that
  spans the whole pass — ensures only one cluster node runs a given pass
  even if two clear the throttle in the same tick. A miss simply idles. A
  *session*-scoped lock is unsafe here: through pgbouncer
  ``pool_mode=transaction`` the backend holding a session lock is recycled
  at each transaction boundary (and with autocommit, that's every
  statement), so a peer's acquire landing on the shared backend gets a
  re-entrant false success and both nodes run. The xact lock is pinned to
  its transaction's backend for the pass and auto-releases on commit.
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
#: constant, namespaced away from the pdf_sha256-derived ingest keys.
_LOCK_KEY = 0x70_61_70_72_65_63_00_01 - 2**63  # "papr ec\x00\x01", mapped signed
#: app_state key holding the ISO-8601 timestamp of the last completed pass.
_STATE_KEY = "paper_reconcile:last_run"


def _refresh_hours() -> float:
    """Minimum gap between full reconcile passes.

    ``PRECIS_PAPER_RECONCILE_REFRESH_HOURS`` (default 24.0, floor 0.1).
    """
    raw = os.environ.get("PRECIS_PAPER_RECONCILE_REFRESH_HOURS")
    if not raw:
        return 24.0
    try:
        return max(0.1, float(raw))
    except ValueError:
        return 24.0


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


def run_paper_reconcile_pass(store: Store, *, limit: int | None = None) -> BatchResult:
    """Run the duplicate reconcilers if due; otherwise no-op.

    ``claimed`` / ``ok`` count duplicate refs merged this pass; ``failed``
    stays 0 (a merge that raises is logged inside the reconciler and just
    doesn't count). Idle passes (throttled or lock-contended) return all
    zeros.
    """
    idle = BatchResult(handler="paper_reconcile", claimed=0, ok=0, failed=0)
    if not store.dsn or not _due(store):
        return idle
    dsn = store.dsn

    from precis.ingest.dedup import (
        TitleMatchReview,
        reconcile_by_doi_case,
        reconcile_by_pdf_sha256,
        reconcile_by_title_similarity,
    )

    # Single-runner lock: hold pg_try_advisory_xact_lock inside ONE open
    # transaction on a dedicated connection for the whole pass. The lock is
    # transaction-scoped, so pgbouncer keeps the backend pinned for the
    # duration and it auto-releases on commit/rollback (no explicit unlock,
    # no leak if the process dies). A session lock would be recycled at the
    # first transaction boundary under transaction pooling — see the module
    # docstring. The dedicated conn only holds the lock; the reconcile work
    # runs on the ``store`` pool.
    conn = psycopg.connect(dsn)
    try:
        with conn.transaction():
            row = conn.execute(
                "SELECT pg_try_advisory_xact_lock(%s)", (_LOCK_KEY,)
            ).fetchone()
            if not (row and row[0]):
                return idle  # another node owns the sweep this cycle

            review: list[TitleMatchReview] = []
            title_outcomes = reconcile_by_title_similarity(
                store, dry_run=False, limit=limit, review_out=review
            )
            pdf_outcomes = reconcile_by_pdf_sha256(store, dry_run=False, limit=limit)
            doi_outcomes = reconcile_by_doi_case(store, dry_run=False, limit=limit)

            # Deterministic hygiene heals (run after the merges so a fresh
            # supersede/soft-delete is picked up the same pass).
            from precis.ingest.paper_hygiene import (
                collapse_superseded_chains,
                heal_drifted_cards,
                migrate_dangling_paper_links,
                requeue_stranded_fetches,
            )

            healed_cards = heal_drifted_cards(store, dry_run=False, limit=limit)
            collapsed = collapse_superseded_chains(store, dry_run=False, limit=limit)
            relinked = migrate_dangling_paper_links(store, dry_run=False, limit=limit)
            requeued = requeue_stranded_fetches(store, dry_run=False, limit=limit)
            store.set_setting(_STATE_KEY, datetime.now(UTC).isoformat())

            merged = sum(
                len(o.duplicate_ref_ids)
                for o in (*title_outcomes, *pdf_outcomes, *doi_outcomes)
            )
            if merged or review or healed_cards or collapsed or relinked or requeued:
                log.info(
                    "paper_reconcile: merged %d duplicate ref(s) "
                    "(%d title, %d pdf_sha256, %d doi-case); %d flagged for review; "
                    "healed %d card(s), collapsed %d chain(s), migrated %d link(s), "
                    "re-queued %d stranded fetch(es)",
                    merged,
                    len(title_outcomes),
                    len(pdf_outcomes),
                    len(doi_outcomes),
                    len(review),
                    len(healed_cards),
                    len(collapsed),
                    len(relinked),
                    len(requeued),
                )
            for r in review:
                log.info("paper_reconcile: %s", r.line())
            work = (
                merged
                + len(healed_cards)
                + len(collapsed)
                + len(relinked)
                + len(requeued)
            )
            return BatchResult(
                handler="paper_reconcile", claimed=work, ok=work, failed=0
            )
    finally:
        conn.close()


__all__ = ["run_paper_reconcile_pass"]

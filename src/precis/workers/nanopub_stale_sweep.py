"""gr279770's scheduled sweep half — fired from the ``nanopub_stale_sweep``
scheduler cadence.

The filter half shipped first (54d47e8c): ``precis.nanopub.stale.
candidate_stale_reason`` + the ``/nanopub`` queue's stale badge, so a
reviewer never mistook a dead row for live work. But a badge alone leaves
the row sitting in ``state='candidate'`` forever — the ``health_digest``
watchdog (``staged_candidates_fresh``) kept re-firing because nothing ever
took the row out of candidacy once it went stale, and the count only
shrank via ad hoc manual cleanup (gr279770 comment 1).

This pass re-runs the identical narrow re-gate
(:func:`precis.nanopub.stale.candidate_stale_reason`) over every live
``candidate`` row and, for the ones that fail it, discards the row
(:meth:`precis.store.Store.nanopub_discard_candidate`) — "routing them back
to the finding lifecycle", the remedy ``health_digest``'s own docstring
already prescribed. Discarding, not tombstoning: the hub simply has no
publish row again, so it re-enters candidacy through the ordinary
:func:`precis.nanopub.mint.approve` staging path the moment someone
re-stages it (a reword that clears the lint, an adjudicated dispute that
resolves) — see :meth:`precis.store.Store.nanopub_discard_candidate` for
why a delete, not a new state. Idempotent by construction: a demoted row
is gone from ``state='candidate'``, so the next fire has nothing left to
re-act on for it.

Deliberately separate from :mod:`precis.nanopub.demote` — that module
walks a *frozen* (``reviewed``/``signed``/…) row back down the ladder when
fresh evidence lands; ``plan_demotion`` maps ``candidate`` to
``ACTION_NONE`` on purpose (nothing is frozen there, so a live
``contradicts`` edge is caught at approve time instead). This sweep is the
other case entirely: a *staged, unfrozen* row that has quietly gone stale
while nobody was looking, which nothing else ever re-checks.

Every demotion is logged (hub id, publish row id, stale reason) and
stamped onto the hub's own ``refs.meta`` (``nanopub_stale_demoted``) — the
same "what happened and when" breadcrumb :mod:`precis.workers.hub_refine`
leaves on its own passes — so the discard is explainable after the fact
without digging through worker logs.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from precis.workers.runner import BatchResult

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: ``refs.meta`` key stamped on a hub whose ``candidate`` row this sweep
#: discarded — one snapshot of the most recent discard (not an
#: accumulating log), matching ``hub_refine``'s
#: ``last_refined_at``/``last_refined_sha`` breadcrumb idiom.
META_STALE_DEMOTED = "nanopub_stale_demoted"


def run_nanopub_stale_sweep_pass(store: Store) -> BatchResult:
    """One sweep tick: re-gate every live ``candidate`` row, discard the
    ones that no longer clear
    :func:`precis.nanopub.stale.candidate_stale_reason`.

    ``claimed`` = candidate rows considered this fire, ``ok`` = rows
    discarded, ``failed`` = rows whose re-gate or discard raised (logged,
    never allowed to stop the rest of the sweep — one poisoned row must
    not strand the others)."""
    from precis.nanopub.stale import candidate_stale_reason

    rows = store.nanopub_candidate_regate_rows()
    demoted = 0
    failed = 0
    for row in rows:
        try:
            reason = candidate_stale_reason(
                canonical=row.canonical,
                disputed=row.disputed,
                title=row.title,
                artifact_type=row.artifact_type,
            )
            if reason is None:
                continue
            if not store.nanopub_discard_candidate(row.publish_id):
                # Lost the race (approved/reopened/re-signed between the
                # read above and here) — the next fire re-reads live state.
                continue
            store.update_ref(
                row.ref_id,
                meta_patch={
                    META_STALE_DEMOTED: {
                        "publish_id": row.publish_id,
                        "reason_kind": reason.kind,
                        "reason_label": reason.label,
                        "artifact_type": row.artifact_type,
                        "at": datetime.now(UTC).isoformat(),
                    }
                },
            )
            demoted += 1
            log.info(
                "nanopub_stale_sweep: discarded candidate row #%d for fi%d (%s: %s)",
                row.publish_id,
                row.ref_id,
                reason.kind,
                reason.label,
            )
        except Exception:
            log.exception(
                "nanopub_stale_sweep: re-gate/discard failed for fi%d (row #%d)",
                row.ref_id,
                row.publish_id,
            )
            failed += 1

    return BatchResult(
        handler="nanopub_stale_sweep", claimed=len(rows), ok=demoted, failed=failed
    )


__all__ = ["META_STALE_DEMOTED", "run_nanopub_stale_sweep_pass"]

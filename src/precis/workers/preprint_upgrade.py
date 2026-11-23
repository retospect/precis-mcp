"""Preprint → published upgrade pass (gr690851) — the background sweep.

Runs :func:`precis.ingest.preprint_upgrade.sweep_preprint_upgrades` over
the cited-preprint cohort so a cited preprint with a discoverable
published version stops being a permanent retraction-watch hole: the
upgrade mints/folds the published paper, links
``published --supersedes--> preprint`` and soft-retires the preprint
(``STATUS:superseded`` tag, ref kept), after which the shared cite
resolver (:mod:`precis.taproot.cite`) and the draft retraction walk
(:mod:`precis.export.retraction`) resolve the cite to the published DOI.

Cheap between runs: a ``preprint_upgrade:last_run`` marker in
``app_state`` gates the pass to once per
``PRECIS_PREPRINT_UPGRADE_REFRESH_HOURS`` (default 24) — the per-ref
``meta.preprint_upgrade_checked_at`` stamp (plus the ``supersedes``-edge
exclusion) already makes the sweep itself convergent, so no advisory lock:
a concurrent run is merely redundant, never wrong. Dark by default (§L:
``service prio`` / ``--only preprint_upgrade`` / a seeded
``PRECIS_PREPRINT_UPGRADE_ENABLED`` row lights it); S2-batch + title-match
with Crossref publication validation.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from functools import partial

from precis.store import Store
from precis.workers import _throttle
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)

#: app_state key holding the ISO-8601 timestamp of the last completed pass.
_STATE_KEY = "preprint_upgrade:last_run"
#: Env var + default for the cadence throttle (see :func:`_throttle.due`).
_REFRESH_ENV_VAR = "PRECIS_PREPRINT_UPGRADE_REFRESH_HOURS"
_DEFAULT_REFRESH_HOURS = 24.0
#: Max refs attempted per pass — a backlog drains over successive passes
#: instead of spending one cycle's whole network budget at once.
_PER_PASS = 100


def _due(store: Store) -> bool:
    """True when the throttle window has elapsed since the last pass."""
    return _throttle.due(store, _STATE_KEY, _REFRESH_ENV_VAR, _DEFAULT_REFRESH_HOURS)


def run_preprint_upgrade_pass(store: Store, *, limit: int | None = None) -> BatchResult:
    """Sweep the cited-preprint cohort if due; otherwise no-op.

    ``claimed`` / ``ok`` count attempted refs; ``failed`` counts only
    unexpected errors — a no-published-version verdict is a successful
    check, not a failure, and a rate-limit deferral stops the pass without
    counting it (gr503080)."""
    idle = BatchResult(handler="preprint_upgrade", claimed=0, ok=0, failed=0)
    if not store.dsn or not _due(store):
        return idle

    from precis.ingest.crossref import fetch_message
    from precis.ingest.preprint_upgrade import (
        V_DEFERRED,
        V_ERROR,
        V_UPGRADED,
        sweep_preprint_upgrades,
    )
    from precis.secrets import get_secret
    from precis.settings import get_str

    result = sweep_preprint_upgrades(
        store,
        limit=limit or _PER_PASS,
        api_key=get_secret("SEMANTIC_SCHOLAR_API_KEY", store=store) or "",
        crossref_fn=partial(
            fetch_message, mailto=get_str("contact.crossref_mailto", store=store) or ""
        ),
    )
    deferred = result.count(V_DEFERRED)
    if not deferred:
        store.set_setting(_STATE_KEY, datetime.now(UTC).isoformat())
    for o in result.outcomes:
        log.info("preprint_upgrade: %s", o.line())
    claimed = len(result.outcomes) - deferred
    failed = result.count(V_ERROR)
    log.info(
        "preprint_upgrade: %d attempted, %d upgraded, %d deferred",
        claimed,
        result.count(V_UPGRADED),
        deferred,
    )
    return BatchResult(
        handler="preprint_upgrade", claimed=claimed, ok=claimed - failed, failed=failed
    )


__all__ = ["run_preprint_upgrade_pass"]

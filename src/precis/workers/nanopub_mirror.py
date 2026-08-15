"""The registry-mirror sync pass — fired from the ``nanopub_mirror``
scheduler cadence (docs/backlog/nanopub-registry-mirror.md).

One fire does, in order:

1. **Delta sync** — PK-diff the registry's code list against
   ``nanopub_mirror``, fetch/verify/index up to the per-pass cap. The
   initial ~87k pull is expected to run through the manual CLI door
   (``precis nanopub mirror sync --live``); this cadence keeps the
   mirror current afterwards (new codes only, a handful a day).
2. **Flag scan** — derive ``retracted_by``/``superseded_by`` under the
   authoritative-retraction rule (idempotent SQL over the edge table).
3. **Concurrence scan** — alert on external nanopubs asserting the same
   AIDA sentence as one of our live publish rows (fingerprint-deduped).

DARK unless ``PRECIS_MIRROR_ENABLED`` (same posture as ``ots_sweep``):
everything here is outbound-read-only network + local writes to the
mirror cache, and even that stays off until Reto turns it on."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from precis.workers.runner import BatchResult

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: Per-pass fetch cap — the steady-state delta is tiny; this only
#: matters if the cadence is enabled before the initial pull finished.
PASS_LIMIT = int(os.environ.get("PRECIS_MIRROR_PASS_LIMIT", "1000"))


def run_mirror_pass(store: Store) -> BatchResult:
    from precis.nanopub import mirror

    if not mirror.mirror_enabled():
        return BatchResult(handler="nanopub_mirror", claimed=0, ok=0, failed=0)

    fetched = failed = 0
    try:
        result = mirror.sync(store, limit=PASS_LIMIT)
        fetched = result.fetched
        failed = result.failed
        if result.remaining:
            log.info(
                "nanopub_mirror: %d codes still missing after this pass",
                result.remaining,
            )
    except Exception:
        log.exception("nanopub_mirror: sync failed")
        failed += 1

    try:
        flagged = store.mirror_apply_flags()
        if flagged:
            log.info("nanopub_mirror: %d rows newly flagged", flagged)
    except Exception:
        log.exception("nanopub_mirror: flag scan failed")
        failed += 1

    try:
        alerts = mirror.concurrence_scan(store)
        if alerts:
            log.info("nanopub_mirror: %d new concurrence alerts", alerts)
    except Exception:
        log.exception("nanopub_mirror: concurrence scan failed")
        failed += 1

    return BatchResult(
        handler="nanopub_mirror", claimed=fetched, ok=fetched, failed=failed
    )

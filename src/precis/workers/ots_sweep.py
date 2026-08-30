"""The daily OTS sweep (nanopub slice 3) — fired from the ``ots_sweep``
scheduler cadence.

One fire does, in order:

1. **topo/drift scan + stamp** — :func:`precis.nanopub.ots.stamp_batch`
   flips dependency-dirty ``signed`` rows back to ``reviewed``, skips
   title-drifted rows, Merkle-batches everything still waiting and
   stamps the root once (one calendar request per fire, ~one per day —
   the spec's decided cadence; granularity caps at 24h, enough when the
   anchor's job is key-rotation scoping).
2. **upgrade sweep** — polls the calendar for pending batches; a
   completed proof INSERTs an ``upgraded`` row (append-only); a batch
   pending past the threshold raises the stuck-pending alert.
3. **recompute audit** — every derived value re-checked against the
   retained bytes (:func:`precis.nanopub.ots.audit`); any mismatch
   raises a ``critical`` alert. Detection without trusting the store,
   run periodically rather than on demand — this cadence IS the period.

DARK unless ``PRECIS_OTS_ENABLED=1`` (the cadence's ``eligible`` gate,
same pattern as ``structural``'s dark switch): steps 1–2 talk to the OTS
calendar — a 32-byte content-blind digest leaves the box, nothing else —
and even that stays off until Reto turns it on. The audit half runs even
when disabled IF proof rows already exist (auditing is free of network
and the store's integrity shouldn't wait on a network flag)."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from precis.workers.runner import BatchResult

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)


def ots_enabled() -> bool:
    return os.environ.get("PRECIS_OTS_ENABLED", "").strip() in ("1", "true", "yes")


def run_ots_sweep_pass(store: Store) -> BatchResult:
    from precis.nanopub import ots

    stamped = 0
    upgraded = 0
    failed = 0

    if ots_enabled():
        calendar = os.environ.get("PRECIS_OTS_CALENDAR", ots.DEFAULT_CALENDAR).strip()
        try:
            if ots.stamp_batch(store, calendar_url=calendar) is not None:
                stamped = 1
        except Exception:
            log.exception("ots_sweep: stamp_batch failed")
            failed += 1
        try:
            upgraded = len(ots.upgrade_sweep(store))
        except Exception:
            log.exception("ots_sweep: upgrade_sweep failed")
            failed += 1

    try:
        findings = ots.audit(store)
        if findings:
            failed += 1
    except Exception:
        log.exception("ots_sweep: audit failed")
        failed += 1

    return BatchResult(
        handler="ots_sweep",
        claimed=stamped + upgraded,
        ok=stamped + upgraded,
        failed=failed,
    )

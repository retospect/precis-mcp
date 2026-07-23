"""The decentralized recurring-work trigger — one scheduler, no singleton.

Slice 10 / §15i of ``docs/design/factory-console-and-scheduling.md``. Today
recurring work has *two* triggers that overlap: the ``schedule`` pass (due
``level:recurring`` Watches) and a set of standalone launchd timers that each
run ``precis <thing>`` on a cadence (``precis cron tick`` @60s,
``precis worker --only watch_poll`` @1h, …). §15i's decision: "there ought to
be only one scheduler", and its exactly-once guarantee belongs in Postgres,
not in a designated node (a SPOF — down when a fire is due ⇒ missed fire).

So this pass folds the thin-timer cadences into the worker itself,
**decentralized**: every worker runs it each cycle, and claiming a due cadence
is an atomic conditional advance on ``scheduler_leases`` (§5.2's
reserve-at-claim, applied to time — :meth:`Store.claim_scheduler_lease`). Only
one worker wins each due cadence; a down worker never drops a fire; a
fleet-wide outage collapses to one catch-up fire on recovery.

**Ships DARK (Phase-1).** The ``scheduler`` service is off by default (no
default profile, ``PRECIS_SCHEDULER_ENABLED`` unset), so the standalone launchd
timers still own the ticks — no double-fire. The Phase-2 window flips the flag
on across the fleet and retires the timers (their launchd plists), leaving this
as the single trigger.

*Trigger is separate from execution* (§15i): today each cadence runs its work
in-process on whichever worker won the lease — fine for the host-agnostic
``cron_tick`` (a ``pg_notify`` asa_bot delivers) and network-only polls. A later
refinement mints a capability-routed job instead of running inline; the lease
mechanism is unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Cadence:
    """One folded thin-timer: a cadence name, its interval, and the work it
    fires. ``run(store, batch_size)`` does the work when this cadence's lease is
    won; its own detail is logged by the underlying pass."""

    name: str
    interval_s: int
    run: Callable[[Any, int], None]


def _run_cron_tick(store: Any, batch_size: int) -> None:
    """Fire due schedule ticks — the §15i cadence, run in-process.

    Historically fired the retired ``kind='cron'`` engine
    (:func:`precis.cli.cron.fire_due_cron`); ADR 0061 folded that push
    mechanism onto ``level:recurring`` (``meta.deliver`` + one-shot
    ``meta.schedule.at``), so this cadence now shares
    :func:`precis.workers.schedule.worker.run_schedule_pass` with the
    launchd ``precis cron tick`` timer and the default worker rotation
    (one implementation, no drift). The cadence name is unchanged —
    it's still "the thing that ticks scheduled work every 60s," the
    underlying kind just moved.
    """
    from precis.workers.schedule.worker import run_schedule_pass

    run_schedule_pass(store, limit=batch_size or 50)


def _run_watch_poll(store: Any, batch_size: int) -> None:
    """Poll S2 for citing papers — the cadenced external acquisition pass that
    today runs via a dedicated ``precis worker --only watch_poll`` launchd
    timer."""
    from precis.workers.watch_poll import run_watch_pass

    run_watch_pass(store, limit=batch_size)


#: The folded cadences (intervals mirror the launchd timers they retire:
#: ``precis-cron-tick`` 60s, ``precis-watch-poll`` 3600s). ``dream`` (hermes-
#: pinned) and ``reconcile`` (caspar-pinned, single-host by design) stay
#: standalone and are deliberately absent. ``anki_sync`` / ``news_poll`` fold
#: identically once each exposes a store-taking callable — a one-line addition
#: here.
CADENCES: tuple[Cadence, ...] = (
    Cadence(name="cron_tick", interval_s=60, run=_run_cron_tick),
    Cadence(name="watch_poll", interval_s=3600, run=_run_watch_poll),
)


def run_scheduler_pass(
    store: Any,
    *,
    host: str,
    batch_size: int = 32,
    cadences: tuple[Cadence, ...] = CADENCES,
) -> BatchResult:
    """Claim + fire every due cadence this cycle. Decentralized: safe to run
    concurrently on every worker — the lease's conditional advance guarantees
    exactly one fire per interval across the fleet.

    ``claimed`` = cadences this worker won this cycle (0 when nothing is due, so
    the loop still idle-sleeps); ``ok`` = cadences that ran clean; ``failed`` =
    cadences whose work raised (the lease already advanced — a raise doesn't
    re-fire until next interval, matching the launchd timer's fire-and-forget).
    """
    claimed = ok = failed = 0

    for cad in cadences:
        try:
            won = store.claim_scheduler_lease(cad.name, cad.interval_s, host)
        except Exception:  # pragma: no cover — a lease blip must not wedge the loop
            log.warning("scheduler: lease claim failed for %s", cad.name, exc_info=True)
            continue
        if not won:
            continue
        claimed += 1
        log.info(
            "scheduler: fired cadence %s (every %ds) on %s",
            cad.name,
            cad.interval_s,
            host,
        )
        try:
            cad.run(store, batch_size)
            ok += 1
        except Exception:
            log.exception("scheduler: cadence %s work raised", cad.name)
            failed += 1

    return BatchResult(handler="scheduler", claimed=claimed, ok=ok, failed=failed)


__all__ = ["CADENCES", "Cadence", "run_scheduler_pass"]

"""The decentralized recurring-work trigger — one scheduler, no singleton.

Slice 10 / §15i of ``docs/design/factory-console-and-scheduling.md``. Today
recurring work has *two* triggers that overlap: the ``schedule`` pass (due
recurring Watches — ``meta.schedule`` set) and a set of standalone launchd timers that each
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

**Live in prod (§A).** The ``scheduler`` pass runs by default on both worker
profiles (``registry.py``'s ``scheduler`` ``ServiceSpec``, ``_SYS + _AGT`` —
the agent profile must run it too, or the host-pinned ``dream_agent`` /
``anki_sync`` cadences below never get an eligible claimant). The standalone
launchd thin-timers it replaces (``cron-tick``, ``watch-poll``, ``dream``,
``anki-sync``, the caspar ``reconcile`` plist) are retired; every comment
elsewhere calling this pass "DARK" is stale.

**Host affinity + local eligibility (§A).** Not every cadence is host-agnostic
like ``cron_tick``/``watch_poll``. A cadence pinned via ``host_affinity`` is
only *attempted* on that host — the claim call itself is skipped elsewhere, so
the lease is never advanced by a host that can't do the work. A cadence's
``eligible`` callable is a further, cheaper-than-the-claim local gate (env/file
presence) checked *before* attempting the claim: an ineligible worker must
never win the lease, or a host that merely happens to run first (e.g.
melchior's *system*-profile worker, which lacks the OAuth/env the dream
cadence needs) would burn the fire the truly-capable process needed. Both
checks are pure short-circuits ahead of :meth:`Store.claim_scheduler_lease` —
skipping them never touches ``scheduler_leases``.

*The affinity contract*: a pinned cadence stalls while its pinned host is
down — that is the contract, not a bug. The lease's ``next_fire_at <=
now()`` + advance-to-``now()+interval`` still gives catch-up-late-not-lost on
recovery (one fire, no backlog burst) once the host returns. Law 6's
no-stall-on-a-down-worker guarantee (docs/proposals/cluster-scheduling.md)
applies to *unpinned* cadences only; §D's staleness alarms are the intended
backstop for a pinned host that's down too long, not this lane.

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
    won; its own detail is logged by the underlying pass.

    ``host_affinity`` (§A) pins the cadence to one host name — every other
    host skips the claim attempt entirely (see the module docstring's
    affinity contract). ``eligible`` (§A) is a cheap local-process gate
    (env/file presence) checked before the claim; an ineligible worker skips
    without touching the lease, so a later eligible claimant still gets the
    fire (drop-no-fire, not a stolen one). ``resolve_interval`` (§A), when
    set, resolves the live interval (seconds) from ``store`` at claim time —
    e.g. a DB-overridable knob — instead of the static ``interval_s``
    (``interval_s`` stays as the cadence's documented/default value)."""

    name: str
    interval_s: int
    run: Callable[[Any, int], None]
    host_affinity: str | None = None
    eligible: Callable[[], bool] | None = None
    resolve_interval: Callable[[Any], int] | None = None


def _run_cron_tick(store: Any, batch_size: int) -> None:
    """Fire due schedule ticks — the §15i cadence, run in-process.

    Historically fired the retired ``kind='cron'`` engine
    (:func:`precis.cli.cron.fire_due_cron`); ADR 0061 folded that push
    mechanism onto recurring todos (``meta.schedule`` set — ``meta.deliver``
    + one-shot ``meta.schedule.at``), so this cadence now shares
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


def _run_dream_agent(store: Any, batch_size: int) -> None:
    """One dream tick, fired from the melchior-pinned ``dream_agent`` cadence
    (§A) instead of the retired standalone 15-min hermes LaunchDaemon.
    ``batch_size`` is unused — ``run_dream_pass`` dispatches exactly one
    reflective-memory tick per fire, same as the old ``--batch-size 1`` the
    launchd wrapper passed. The inner ``BatchResult`` is logged so a
    lease fire that dream internally gated (too-soon knob, high load,
    missing prompt) is distinguishable from a real dispatch — the
    scheduler's own ``ok`` counter only says the callable didn't raise."""
    from precis.workers.dream_agent import run_dream_pass

    result = run_dream_pass(store)
    log.info(
        "scheduler: dream_agent inner result claimed=%d ok=%d failed=%d",
        result.claimed,
        result.ok,
        result.failed,
    )


def _run_anki_sync(store: Any, batch_size: int) -> None:
    """One AnkiWeb sync tick, fired from the melchior-pinned ``anki_sync``
    cadence (§A) instead of the retired standalone 30-min launchd timer.
    Reads the fix/project flags from config (env), matching the plist's
    ``PRECIS_ANKI_FIX_ENABLED`` / ``PRECIS_ANKI_PROJECT_ENABLED``; the
    pg advisory lock in :func:`precis.workers.anki_sync.run_anki_sync` still
    serializes against a concurrent manual ``precis anki-sync`` run."""
    from precis.config import load_config
    from precis.workers.anki_sync import run_anki_sync

    cfg = load_config()
    summary = run_anki_sync(
        store, cfg, fix=cfg.anki_fix_enabled, project=cfg.anki_project_enabled
    )
    log.info("scheduler: anki_sync — %s", summary)


def _run_health_digest(store: Any, batch_size: int) -> None:
    """One §D liveness-net eval, fired from the host-agnostic
    ``health_digest`` cadence (any live worker can win it — unpinned, like
    ``cron_tick``/``watch_poll``). ``batch_size`` is unused; the pass
    evaluates every check each fire, same shape as ``dream_agent``'s
    single-tick-per-fire cadence work."""
    from precis.workers.health_digest import run_health_digest_pass

    result = run_health_digest_pass(store)
    log.info(
        "scheduler: health_digest inner result claimed=%d ok=%d failed=%d",
        result.claimed,
        result.ok,
        result.failed,
    )


def _dream_agent_eligible() -> bool:
    from precis.workers.dream_agent import eligible

    return eligible()


def _anki_sync_eligible() -> bool:
    """The same gate the CLI checks (``PRECIS_ANKI_ENABLED``) plus the
    optional ``anki`` wheel actually being importable on this process — a
    cheap ``find_spec`` probe, not a real import (the pylib is heavy)."""
    import importlib.util

    from precis.config import load_config

    return (
        bool(load_config().anki_enabled)
        and importlib.util.find_spec("anki") is not None
    )


def _dream_resolve_interval(store: Any) -> int:
    from precis.workers import dream_throttle

    return int(dream_throttle.resolve_min_interval_minutes(store) * 60)


#: The folded cadences. ``cron_tick``/``watch_poll`` intervals mirror the
#: launchd timers they retire (``precis-cron-tick`` 60s, ``precis-watch-poll``
#: 3600s) — host-agnostic, no affinity/eligibility needed. ``dream_agent`` /
#: ``anki_sync`` (§A) are host-pinned to melchior (the OAuth/anki-wheel host)
#: with an ``eligible`` gate so only the *agent*-profile process there (which
#: carries the env) ever wins them — melchior's system-profile worker is
#: ineligible and skips without touching the lease. ``dream_agent``'s
#: interval is resolved live from the §G knob (DB > env > compiled 15min) via
#: ``resolve_interval``; ``interval_s`` below is just its documented default.
#: ``reconcile`` (the caspar nightly duplicate sweep) has **no** cadence here
#: — its plist is redundant with the existing ``paper_reconcile`` worker pass
#: (its own 24h ``app_state`` throttle + advisory lock, already fleet-safe),
#: so folding it is a pure *retirement* of the plist, not a new cadence.
#: Migrating ``paper_reconcile``'s own throttle onto the lease is §E, not §A.
#: ``news_poll`` folds identically once it exposes a store-taking callable —
#: a one-line addition here.
CADENCES: tuple[Cadence, ...] = (
    Cadence(name="cron_tick", interval_s=60, run=_run_cron_tick),
    Cadence(name="watch_poll", interval_s=3600, run=_run_watch_poll),
    # §D (docs/proposals/health-watchdog.md): the liveness-net digest.
    # Host-agnostic like cron_tick/watch_poll — any live worker can win it;
    # §A's lease machinery IS the fleet-singleton throttle, so
    # workers/health_digest.py doesn't invent its own.
    Cadence(name="health_digest", interval_s=3600, run=_run_health_digest),
    Cadence(
        name="dream_agent",
        interval_s=15 * 60,
        run=_run_dream_agent,
        host_affinity="melchior",
        eligible=_dream_agent_eligible,
        resolve_interval=_dream_resolve_interval,
    ),
    Cadence(
        name="anki_sync",
        interval_s=1800,
        run=_run_anki_sync,
        host_affinity="melchior",
        eligible=_anki_sync_eligible,
    ),
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

    A cadence with an unmet ``host_affinity`` or a failing ``eligible`` gate
    is skipped *before* the claim attempt (see the module docstring) — it
    counts toward neither ``claimed`` nor ``failed``, same as an undue
    cadence.

    ``claimed`` = cadences this worker won this cycle (0 when nothing is due, so
    the loop still idle-sleeps); ``ok`` = cadences that ran clean; ``failed`` =
    cadences whose work raised (the lease already advanced — a raise doesn't
    re-fire until next interval, matching the launchd timer's fire-and-forget).
    """
    claimed = ok = failed = 0

    for cad in cadences:
        if cad.host_affinity is not None and host != cad.host_affinity:
            continue
        if cad.eligible is not None:
            try:
                is_eligible = cad.eligible()
            except Exception:  # pragma: no cover — a gate blip must not wedge the loop
                log.warning(
                    "scheduler: eligibility check failed for %s",
                    cad.name,
                    exc_info=True,
                )
                continue
            if not is_eligible:
                continue
        interval_s = cad.interval_s
        if cad.resolve_interval is not None:
            try:
                interval_s = cad.resolve_interval(store)
            except Exception:  # a resolver blip must not starve later cadences
                log.warning(
                    "scheduler: interval resolution failed for %s; falling back to %ds",
                    cad.name,
                    cad.interval_s,
                    exc_info=True,
                )
        try:
            won = store.claim_scheduler_lease(cad.name, interval_s, host)
        except Exception:  # pragma: no cover — a lease blip must not wedge the loop
            log.warning("scheduler: lease claim failed for %s", cad.name, exc_info=True)
            continue
        if not won:
            continue
        claimed += 1
        log.info(
            "scheduler: fired cadence %s (every %ds) on %s",
            cad.name,
            interval_s,
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

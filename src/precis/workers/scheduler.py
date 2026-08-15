"""The decentralized recurring-work trigger — one scheduler, no singleton.

Slice 10 / §15i of ``docs/backlog/factory-console-and-scheduling.md``. Today
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
the lease is never *advanced* by a host that can't do the work. A cadence's
``eligible`` callable is a further, cheaper-than-the-claim local gate (env/file
presence) checked *before* attempting the claim: an ineligible worker must
never win the lease, or a host that merely happens to run first (e.g.
melchior's *system*-profile worker, which lacks the OAuth/env the dream
cadence needs) would burn the fire the truly-capable process needed. Both
checks are short-circuits ahead of :meth:`Store.claim_scheduler_lease` — the
claim (and its win) is skipped, but the row is still *seeded*
(:meth:`Store.seed_scheduler_lease`, gr194430) unconditionally, before either
gate: health_digest's cadence-staleness check (§D) iterates existing
``scheduler_leases`` rows only, so a cadence whose ``eligible`` gate reads
False on every host in the fleet (e.g. a deploy regression that drops the
enable env everywhere) must still have a row to go stale and alarm on — a
row that's never advanced, not a row that's absent.

*The affinity contract*: a pinned cadence stalls while its pinned host is
down — that is the contract, not a bug. The lease's ``next_fire_at <=
now()`` + advance-to-``now()+interval`` still gives catch-up-late-not-lost on
recovery (one fire, no backlog burst) once the host returns. Law 6's
no-stall-on-a-down-worker guarantee (docs/backlog/cluster-scheduling.md)
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
from typing import TYPE_CHECKING, Any

from precis.workers.runner import BatchResult

if TYPE_CHECKING:
    from precis.store.store import Store

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
    (``interval_s`` stays as the cadence's documented/default value).

    ``spends`` marks a cadence whose work bills an LLM, so it is gated on the
    global daily ceiling — see :func:`_over_daily_budget`."""

    name: str
    interval_s: int
    run: Callable[[Any, int], None]
    host_affinity: str | None = None
    eligible: Callable[[], bool] | None = None
    resolve_interval: Callable[[Any], int] | None = None
    spends: bool = False


def _run_cron_tick(store: Store, batch_size: int) -> None:
    """Fire due schedule ticks — the §15i cadence, run in-process.

    Historically fired the retired ``kind='cron'`` engine
    (:func:`precis.cli.cron.fire_due_cron`); the cron-into-recurring fold moved that push
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


def _run_watch_poll(store: Store, batch_size: int) -> None:
    """Poll S2 for citing papers — the cadenced external acquisition pass that
    today runs via a dedicated ``precis worker --only watch_poll`` launchd
    timer."""
    from precis.workers.watch_poll import run_watch_pass

    run_watch_pass(store, limit=batch_size)


def _run_dream_agent(store: Store, batch_size: int) -> None:
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


def _run_draft_refresh_scan(store: Store, batch_size: int) -> None:
    """One draft_refresh scan tick (docs/backlog/draft-refresh.md Part 2),
    fired from the host-agnostic ``draft_refresh_scan`` cadence — any live
    worker can win it, same as ``health_digest``/``materialize``.
    ``batch_size`` caps how many jobs get minted this fire (defensive;
    opted-in drafts are few). Minting itself is free (no LLM call — the
    spend lives in the ``draft_refresh`` job it mints), so ``spends`` is
    unset, same reasoning as ``cron_tick`` only spawning child todos."""
    from precis.workers.draft_refresh_scan import run_draft_refresh_scan

    result = run_draft_refresh_scan(store, batch_size)
    log.info(
        "scheduler: draft_refresh_scan inner result claimed=%d ok=%d failed=%d",
        result.claimed,
        result.ok,
        result.failed,
    )


def _run_anki_sync(store: Store, batch_size: int) -> None:
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


def _run_health_digest(store: Store, batch_size: int) -> None:
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


def _run_materialize(store: Store, batch_size: int) -> None:
    """One demand-materializer tick (§F cycle a, ``docs/backlog/cluster-scheduling.md`` §F), fired from the host-agnostic
    ``materialize`` cadence (any live worker can win it — unpinned, like
    ``health_digest``). ``batch_size`` is unused. DARK unless
    ``PRECIS_MATERIALIZE_EMBED=1`` — see workers/materialize.py."""
    from precis.workers.materialize import run_materialize_pass

    result = run_materialize_pass(store)
    log.info(
        "scheduler: materialize inner result claimed=%d ok=%d failed=%d",
        result.claimed,
        result.ok,
        result.failed,
    )


def _run_ots_sweep(store: Store, batch_size: int) -> None:
    """One nanopub OTS sweep (stamp waiting artifacts → upgrade pending
    proofs → recompute audit), fired from the daily host-agnostic
    ``ots_sweep`` cadence. ``batch_size`` is unused — the pass batches
    everything waiting into one Merkle root by design (one calendar
    request per fire). See workers/ots_sweep.py; calendar traffic is
    gated by PRECIS_OTS_ENABLED, the audit runs regardless."""
    from precis.workers.ots_sweep import run_ots_sweep_pass

    result = run_ots_sweep_pass(store)
    log.info(
        "scheduler: ots_sweep inner result claimed=%d ok=%d failed=%d",
        result.claimed,
        result.ok,
        result.failed,
    )


def _run_structural(
    # test_scheduler_pass.py's wrapper-only unit test calls this directly
    # with a bare sentinel object() (the downstream pass is monkeypatched
    # out entirely), narrower than Store.
    store: Any,
    batch_size: int,
) -> None:
    """One structural-review tick, fired from the host-agnostic
    ``structural`` cadence (gr192752) instead of the old default-rotation
    slot on the agent-profile worker. ``batch_size`` is unused — the review
    driver's own 5h dedup window (``STRUCTURAL.min_interval_hours``) is the
    true clock; this cadence is just the check tick, so a deduped fire is a
    cheap no-op query. The inner ``BatchResult`` is logged for the same
    reason as ``dream_agent``'s: distinguish an internally-gated no-op from
    a real dispatch."""
    from precis.workers.structural import run_structural_pass

    result = run_structural_pass(store)
    log.info(
        "scheduler: structural inner result claimed=%d ok=%d failed=%d",
        result.claimed,
        result.ok,
        result.failed,
    )


def _run_deep_review(
    # see _run_structural -- same wrapper-only test, bare sentinel object()
    store: Any,
    batch_size: int,
) -> None:
    """One deep-review tick, fired from the host-agnostic ``deep_review``
    cadence (gr192752) instead of the old default-rotation slot on the
    agent-profile worker. ``batch_size`` is unused — the review driver's own
    144h dedup window (``DEEP_REVIEW.min_interval_hours``) is the true
    clock; this cadence is just the check tick, same shape as
    ``_run_structural``."""
    from precis.workers.deep_review import run_deep_review_pass

    result = run_deep_review_pass(store)
    log.info(
        "scheduler: deep_review inner result claimed=%d ok=%d failed=%d",
        result.claimed,
        result.ok,
        result.failed,
    )


def _structural_eligible() -> bool:
    """The SAME gate ``run_structural_pass`` checks internally
    (``precis.workers.review._gate_enabled``) — a host without
    ``PRECIS_STRUCTURAL_REVIEW`` set skips *before* the claim, so the lease
    is never advanced by a host that can't do the work (drop-no-fire, not a
    stolen fire — a later eligible host still gets it)."""
    from precis.workers.review import _gate_enabled
    from precis.workers.structural import STRUCTURAL

    return _gate_enabled(STRUCTURAL.gate_env)


def _deep_review_eligible() -> bool:
    """Same shape as ``_structural_eligible`` — reuses the review driver's
    gate helper against ``PRECIS_DEEP_REVIEW``."""
    from precis.workers.deep_review import DEEP_REVIEW
    from precis.workers.review import _gate_enabled as _review_gate_enabled

    return _review_gate_enabled(DEEP_REVIEW.gate_env)


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


def _dream_resolve_interval(store: Store) -> int:
    from precis.workers import dream_throttle

    return int(dream_throttle.resolve_min_interval_minutes(store) * 60)


def _over_daily_budget(store: Store, cadence: str) -> bool:
    """True when a ``spends=True`` cadence must skip this fire — the fleet has
    already burned ``PRECIS_DAILY_COST_CEILING`` over the trailing 24h.

    Same number the dispatcher gates on
    (:func:`precis.workers.planner_guardrails.daily_budget`), deliberately:
    before this check the ceiling gated *only* the planner, so a tripped
    envelope froze the cheap user-facing lane while the three expensive opus
    cadences here spent right through it. Prod ran exactly that inversion for
    18h from 2026-08-06 19:02 — 542 "daily ceiling hit" warnings from the
    dispatcher while ``dream_agent`` kept billing ~$0.40/h.

    **Checked after the lease is won, not before.** The other pre-claim gates
    (``host_affinity`` / ``eligible``) exist so a host that *can't* do the work
    leaves the fire for one that can. Budget is fleet-global — no other host
    would pass either — so there is nothing to leave, and consuming the lease
    is what keeps ``next_fire_at`` advancing. Skipping the claim instead would
    park the lease in the past for the whole freeze and trip §D's
    cadence-staleness alarm on all three cadences, reporting a stall whose real
    cause is the budget. It also means the query runs once per actual fire
    (≈5/h fleet-wide) rather than once per worker cycle.

    Fails **closed**, like the dispatcher's guardrail call: a cost gate that
    errors must not wave the spend through. The cost of that choice here is one
    skipped tick on a cadence with its own internal dedup window, which the
    next interval recovers.
    """
    from precis.workers import planner_guardrails

    try:
        budget = planner_guardrails.daily_budget(store)
    except Exception:
        log.exception(
            "scheduler: budget check failed for cadence %s; skipping this fire "
            "(fail-closed)",
            cadence,
        )
        return True
    if not budget.over:
        return False
    log.warning(
        "scheduler: skipping cadence %s — daily cost ceiling hit (%s). "
        "This fire is dropped, not deferred; the next one lands after the "
        "rolling 24h window clears.",
        cadence,
        budget,
    )
    return True


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
#:
#: ``spends=True`` marks the three cadences that bill an LLM, so they honour
#: the same daily ceiling the dispatcher does (:func:`_over_daily_budget`).
#: The rest are deliberately unmarked: ``watch_poll``/``anki_sync`` are network
#: polls, ``health_digest`` is SQL, ``materialize`` is local embeddings, and
#: ``cron_tick`` only *spawns child todos* — those become dispatch candidates
#: and are gated there, at mint, so marking this cadence too would double-gate
#: the same spend.
CADENCES: tuple[Cadence, ...] = (
    Cadence(name="cron_tick", interval_s=60, run=_run_cron_tick),
    Cadence(name="watch_poll", interval_s=3600, run=_run_watch_poll),
    # docs/backlog/self-healing-spine.md Layer 2: the liveness-net digest.
    # Host-agnostic like cron_tick/watch_poll — any live worker can win it;
    # §A's lease machinery IS the fleet-singleton throttle, so
    # workers/health_digest.py doesn't invent its own.
    Cadence(name="health_digest", interval_s=3600, run=_run_health_digest),
    # §F cycle a: the demand materializer. Host-agnostic like health_digest
    # — any live worker can win it. Default-ON; PRECIS_MATERIALIZE_EMBED=0
    # opts out (workers/materialize.py::_materialize_enabled); this cadence
    # firing every 5 min is itself harmless — the pass no-ops when disabled.
    Cadence(name="materialize", interval_s=300, run=_run_materialize),
    # gr192752: the two opus reviewers, migrated off the agent-profile
    # default rotation. Under `--profile all` a long `chase` pass
    # (PassBand.BACKGROUND, registered first) can monopolize the strictly-
    # serial rotation for hours (synchronous S2 lookups, tenacity retries),
    # starving both reviewers — observed 85-min starvation on the
    # inference host. NO `host_affinity` here (unlike dream_agent/
    # anki_sync) — eligibility is purely the env gate
    # (PRECIS_STRUCTURAL_REVIEW / PRECIS_DEEP_REVIEW), which the collapsed-
    # unit deploy template scopes to TWO hosts (gateway + inference). That
    # is the actual fix: the lease is fleet-wide, so one host's wedged
    # rotation can no longer starve a reviewer — the other eligible host's
    # scheduler pass wins the fire instead. The interval below is just the
    # check tick, not the true clock — the review driver's own internal
    # dedup (5h structural / 144h deep_review, see STRUCTURAL/DEEP_REVIEW's
    # `min_interval_hours`) still gates the actual review, so a deduped
    # fire is a cheap no-op query and the real review lands within one
    # tick of the window expiring, mirroring the old rotation's continuous
    # attempts.
    Cadence(
        name="structural",
        interval_s=3600,
        run=_run_structural,
        eligible=_structural_eligible,
        spends=True,
    ),
    Cadence(
        name="deep_review",
        interval_s=6 * 3600,
        run=_run_deep_review,
        eligible=_deep_review_eligible,
        spends=True,
    ),
    Cadence(
        name="dream_agent",
        interval_s=15 * 60,
        run=_run_dream_agent,
        host_affinity="melchior",
        eligible=_dream_agent_eligible,
        resolve_interval=_dream_resolve_interval,
        spends=True,
    ),
    Cadence(
        name="anki_sync",
        interval_s=1800,
        run=_run_anki_sync,
        host_affinity="melchior",
        eligible=_anki_sync_eligible,
    ),
    # docs/backlog/draft-refresh.md Part 2: the living-draft staleness
    # scanner. Host-agnostic like health_digest/materialize — the lease
    # IS the fleet-singleton throttle. Off the exact hour (4h7m) so it
    # doesn't pile onto the same tick as the hourly cadences above.
    # spends=False: minting is free; the LLM spend lives in the
    # draft_refresh job this cadence mints, gated there at dispatch.
    Cadence(
        name="draft_refresh_scan",
        interval_s=4 * 3600 + 7 * 60,
        run=_run_draft_refresh_scan,
    ),
    # Nanopub slice 3: the daily anchor sweep (spec: one cron, decided
    # 2026-08-13). Host-agnostic — the lease is the fleet-singleton
    # throttle. Off the exact day boundary (24h11m) like
    # draft_refresh_scan's off-hour convention. No `eligible` env gate:
    # the pass itself gates calendar traffic on PRECIS_OTS_ENABLED and
    # still runs its (local, free) recompute audit when dark — the proof
    # store's integrity check shouldn't wait on a network flag.
    # spends=False: no LLM anywhere in the sweep.
    Cadence(
        name="ots_sweep",
        interval_s=24 * 3600 + 11 * 60,
        run=_run_ots_sweep,
    ),
)


def run_scheduler_pass(
    # forwards into the Store-typed _run_*/_over_daily_budget helpers;
    # test_scheduler_budget_gate.py calls this directly with a minimal
    # _Store (only claim_scheduler_lease) narrower than Store.
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
    cadence. A ``spends`` cadence over the daily cost ceiling is skipped
    *after* the claim (:func:`_over_daily_budget` explains why) and so counts
    as ``claimed`` with neither ``ok`` nor ``failed``.

    ``claimed`` = cadences this worker won this cycle (0 when nothing is due, so
    the loop still idle-sleeps); ``ok`` = cadences that ran clean; ``failed`` =
    cadences whose work raised (the lease already advanced — a raise doesn't
    re-fire until next interval, matching the launchd timer's fire-and-forget).
    """
    claimed = ok = failed = 0

    for cad in cadences:
        # gr194430: seed the row BEFORE either gate, unconditionally — a
        # cadence whose eligible() (or host_affinity) is never satisfied
        # anywhere in the fleet (e.g. a deploy regression dropping the
        # enable env fleet-wide) must still surface a scheduler_leases row
        # for health_digest's cadence-staleness check to go stale on; a
        # gated cadence that never seeds is invisible to that alarm.
        # Seeded with the cadence's static interval_s, not resolve_interval
        # (which may be a callable with DB-query side effects) — fine for
        # staleness margins, which don't need the live-resolved value.
        try:
            store.seed_scheduler_lease(cad.name, cad.interval_s)
        except Exception:  # pragma: no cover — a seed blip must not wedge the loop
            log.warning("scheduler: lease seed failed for %s", cad.name, exc_info=True)
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
        if cad.spends and _over_daily_budget(store, cad.name):
            # Won the lease, spent nothing: counted as claimed but neither ok
            # nor failed, since the work never ran. _over_daily_budget logs the
            # reason — that WARNING is the only signal a fire was dropped.
            continue
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

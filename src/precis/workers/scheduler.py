"""The decentralized recurring-work trigger — one scheduler, no singleton.

Design: ``docs/backlog/factory-console-and-scheduling.md`` §15i (Slice 10)
— its exactly-once guarantee belongs in Postgres, not a designated node
(a SPOF: down when a fire is due ⇒ missed fire).

Folds every recurring-work timer into the worker rotation itself: every
worker runs this pass each cycle, and claiming a due cadence is an atomic
conditional advance on ``scheduler_leases`` (:meth:`Store.claim_scheduler_lease`).
Only one worker wins each due cadence; a down worker never drops a fire; a
fleet-wide outage collapses to one catch-up fire on recovery. Retires the
standalone launchd thin-timers (``cron-tick``, ``watch-poll``, ``dream``,
``anki-sync``, caspar's ``reconcile`` plist) it replaced.

Runs by default on both worker profiles (registry's ``scheduler``
``ServiceSpec``, ``_SYS + _AGT``) — the agent profile must run it too, or
host-pinned ``dream_agent``/``anki_sync`` cadences never get an eligible
claimant.

**Host affinity + local eligibility.** A cadence pinned via
``host_affinity`` is only *attempted* on that host — elsewhere the claim
call is skipped, so the lease is never advanced by a host that can't do
the work. A cadence's ``eligible`` callable is a cheaper local gate
(env/file presence) checked before the claim attempt, so an ineligible
worker never wins the lease over the truly-capable one. Both are
short-circuits ahead of :meth:`Store.claim_scheduler_lease` — but the row
is still *seeded* (:meth:`Store.seed_scheduler_lease`) unconditionally
first, so health_digest's cadence-staleness check (which iterates existing
rows only) can still alarm on a cadence whose ``eligible`` gate reads
False fleet-wide.

*Affinity contract*: a pinned cadence stalls while its pinned host is down
— that's the contract, not a bug (unpinned cadences get the no-stall
guarantee instead; a down-too-long pinned host is health_digest's alarm to
catch). ``next_fire_at <= now()`` + advance-to-``now()+interval`` still
gives catch-up-late-not-lost on recovery (one fire, no backlog burst).

*Trigger is separate from execution*: today each cadence runs its work
in-process on whichever worker won the lease. A later refinement could
mint a capability-routed job instead; the lease mechanism is unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
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


def _doctor_tick_window(now: datetime) -> str:
    """The ``<UTC date>/<window>`` idem_key half for the current 8h slice
    of the day — ``window`` = ``hour // 8`` (``0``, ``1``, ``2``), so
    exactly one queued ``doctor_tick`` job can exist per 8h window."""
    return f"{now:%Y-%m-%d}/{now.hour // 8}"


def _run_doctor_tick_mint(store: Store, batch_size: int) -> None:
    """Mint ONE queued ``doctor_tick`` job for the current 8h window
    (``docs/backlog/doctor-tick-report.md`` item 1: the self-healing
    spine's Layer-3 doctor), fired from the host-agnostic ``doctor_tick``
    cadence — any live worker can win the lease, like
    ``health_digest``/``draft_refresh_scan``; execution lands on whichever
    ``claude_inproc`` (gateway) worker claims the minted job, same as
    every other agent-lane job. ``batch_size`` is unused — the idem_key
    IS the per-window cap.

    Minting reuses the direct-``insert_ref`` + idem_key-guarded pattern
    :func:`_run_draft_refresh_scan`/:func:`precis.workers.materialize._mint_jobs`
    established: parentless (system-minted background maintenance, like
    ``draft_refresh``), any status blocks a re-mint for the same key.
    ``spends=False`` follows the ``draft_refresh_scan`` precedent below,
    not a new choice: minting itself is free (no LLM call happens here),
    so a budget freeze must not silently skip the mint — the minted job's
    own dispatch is where the daily ceiling actually gates the spend,
    the same as every other ``claude_inproc`` job.
    """
    del batch_size
    from precis.store.types import Tag

    now = datetime.now(UTC)
    idem_key = f"doctor:{_doctor_tick_window(now)}"
    with store.pool.connection() as conn:
        existing = conn.execute(
            "SELECT 1 FROM refs WHERE kind = 'job' AND deleted_at IS NULL "
            "AND meta->>'idem_key' = %s LIMIT 1",
            (idem_key,),
        ).fetchone()
        if existing is not None:
            conn.commit()
            return
        ref = store.insert_ref(
            kind="job",
            slug=None,
            title=f"doctor_tick ({idem_key})",
            meta={
                "job_type": "doctor_tick",
                "executor": "claude_inproc",
                "params": {},
                "idem_key": idem_key,
            },
            prio=8,  # background maintenance — same as draft_refresh_scan
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "queued"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        conn.commit()
    log.info("scheduler: minted doctor_tick job for window %s", idem_key)


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


def _run_parts_refresh(store: Store, batch_size: int) -> None:
    """One JLCPCB catalog Open API cursor-walk tick (gr264357), fired from
    the daily host-agnostic ``parts_refresh`` cadence — any live worker can
    win it, same shape as ``ots_sweep``/``nanopub_mirror``. ``batch_size``
    is unused — the pass caps itself via its own row budget (workers/
    parts_refresh.py's ``DEFAULT_ROW_BUDGET``), checkpointed in
    ``app_state`` so an interrupted or resumed walk continues rather than
    restarting the ~7M-row catalog. Dark (clean no-op) without JLCPCB Open
    API credentials on this host — the community-dump bulk load stays a
    manual ``precis pcb refresh-parts --from-sqlite`` call, not this
    cadence."""
    from precis.workers.parts_refresh import run_parts_refresh_pass

    result = run_parts_refresh_pass(store)
    log.info(
        "scheduler: parts_refresh — claimed=%d ok=%d failed=%d",
        result["claimed"],
        result["ok"],
        result["failed"],
    )


def _run_nanopub_mirror(store: Store, batch_size: int) -> None:
    """One registry-mirror pass (delta sync → flag scan → concurrence
    alerts), fired from the daily host-agnostic ``nanopub_mirror``
    cadence. ``batch_size`` is unused — the pass caps its own fetches
    (PRECIS_MIRROR_PASS_LIMIT). See workers/nanopub_mirror.py; all
    network + writes are gated by PRECIS_MIRROR_ENABLED."""
    from precis.workers.nanopub_mirror import run_mirror_pass

    result = run_mirror_pass(store)
    log.info(
        "scheduler: nanopub_mirror inner result claimed=%d ok=%d failed=%d",
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
    # (PassPriority.BACKGROUND, registered first) can monopolize the strictly-
    # serial rotation for hours (synchronous S2 lookups, tenacity retries),
    # starving both reviewers — observed 85-min starvation on the
    # inference host. NO `host_affinity` here (unlike dream_agent/
    # anki_sync) — eligibility is purely the dark switch
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
    # docs/backlog/doctor-tick-report.md item 1 (self-healing spine Layer
    # 3): mints one queued doctor_tick job per 8h window (the spec's
    # 6-12h band). Host-agnostic like draft_refresh_scan — the lease IS
    # the fleet-singleton throttle. spends=False: minting is free; the
    # LLM spend lives in the doctor_tick job this cadence mints, gated
    # there at dispatch like every claude_inproc job — see
    # _run_doctor_tick_mint for why this diverges from a naive
    # spends=True read of "this cadence leads to an LLM call".
    Cadence(
        name="doctor_tick",
        interval_s=8 * 3600,
        run=_run_doctor_tick_mint,
    ),
    # Nanopub slice 3: the daily anchor sweep (spec: one cron, decided
    # 2026-08-13). Host-agnostic — the lease is the fleet-singleton
    # throttle. Off the exact day boundary (24h11m) like
    # draft_refresh_scan's off-hour convention. No `eligible` check:
    # the pass itself gates calendar traffic on PRECIS_OTS_ENABLED and
    # still runs its (local, free) recompute audit when dark — the proof
    # store's integrity check shouldn't wait on a network flag.
    # spends=False: no LLM anywhere in the sweep.
    Cadence(
        name="ots_sweep",
        interval_s=24 * 3600 + 11 * 60,
        run=_run_ots_sweep,
    ),
    # Same posture as ots_sweep: no `eligible` check — the pass
    # itself no-ops unless PRECIS_MIRROR_ENABLED, so the cadence lease
    # advances harmlessly while dark. Offset from ots_sweep's +11m so
    # the two daily nanopub fires don't land together. spends=False:
    # no LLM anywhere in the sync.
    Cadence(
        name="nanopub_mirror",
        interval_s=24 * 3600 + 23 * 60,
        run=_run_nanopub_mirror,
    ),
    # gr264357: the JLCPCB catalog ingest — daily, off the exact day
    # boundary like ots_sweep/nanopub_mirror above so the three daily
    # fires don't land together. Host-agnostic; no `eligible` check —
    # the pass itself no-ops cleanly without JLCPCB API credentials (see
    # workers/parts_refresh.py), same posture as ots_sweep/nanopub_mirror.
    # spends=False: no LLM anywhere in the walk.
    Cadence(
        name="parts_refresh",
        interval_s=24 * 3600 + 37 * 60,
        run=_run_parts_refresh,
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

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
worker never wins the lease over the truly-capable one. A cadence naming
``service`` gates the same way on that service's per-host resolved
``service_config`` prio — a host dimmed to 0 skips the claim so a lit
host still gets the fire. All are
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

*Two triggers, one mechanism* (gr518349): the pass fires from a dedicated
``start_scheduler_thread`` daemon thread on its own ~5s tick — the standing
trigger, because one long serial rotation step (hub_refine ~10 min on
the agent host) must not starve every due cadence — plus the in-rotation
``scheduler`` ref-pass kept as a backstop. Lease claims are atomic
conditional advances, so both triggers firing in the same window is a
harmless ``claimed=0`` for the loser, never a double fire — the same
argument as the heartbeat thread vs its in-rotation pass.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

from precis.utils.deadline import DeadlineExceeded, call_with_deadline
from precis.utils.env import env_float
from precis.workers.runner import BatchResult

if TYPE_CHECKING:
    from precis.store.store import Store
    from precis.workers.service_config import ServiceConfigResolver

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

    ``service`` names the ``service_config`` row whose per-host resolved
    ``prio`` gates the claim, resolved the
    :class:`~precis.workers.service_config.ServiceConfigResolver` way
    (exact-host row beats ``*``): a host resolving to 0 — or to nothing,
    a service-gated cadence is dark until lit — skips before the claim,
    so the fire stays claimable by a lit host (gr518089: a fleet-wide
    ``*`` light must not let a host an operator dimmed win the tick).

    ``spends`` marks a cadence whose work bills an LLM, so it is gated on the
    global daily ceiling — see :func:`_over_daily_budget`."""

    name: str
    interval_s: int
    run: Callable[[Any, int], None]
    host_affinity: str | None = None
    eligible: Callable[[], bool] | None = None
    resolve_interval: Callable[[Any], int] | None = None
    service: str | None = None
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


def _run_gripe_parents(store: Store, batch_size: int) -> None:
    """Fill ``meta.dup_parents`` on freshly embedded gripes — pipeline
    v2 slice 2 (``docs/backlog/gripe-pipeline-v2.md`` §Intake). Host-
    agnostic like ``materialize``: the lease is the fleet-singleton
    throttle, and the pass itself is one indexed ANN query per claimed
    gripe — ``spends`` stays False, no LLM and no embedder call."""
    from precis.workers.gripe_parents import run_gripe_parents_pass

    result = run_gripe_parents_pass(store, batch_size)
    log.info(
        "scheduler: gripe_parents inner result claimed=%d written=%d "
        "no_hits=%d failed=%d",
        result.claimed,
        result.written,
        result.no_hits,
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
    from precis.workers._mint import mint_background_job

    now = datetime.now(UTC)
    idem_key = f"doctor:{_doctor_tick_window(now)}"
    with store.pool.connection() as conn:
        existing = conn.execute(
            "SELECT 1 FROM refs WHERE kind = 'job' AND retired_at IS NULL "
            "AND meta->>'idem_key' = %s LIMIT 1",
            (idem_key,),
        ).fetchone()
        if existing is not None:
            conn.commit()
            return
        mint_background_job(
            store,
            conn=conn,
            job_type="doctor_tick",
            executor="claude_inproc",
            title=f"doctor_tick ({idem_key})",
            params={},
            idem_key=idem_key,
            prio=8,  # background maintenance — same as draft_refresh_scan
        )
        conn.commit()
    log.info("scheduler: minted doctor_tick job for window %s", idem_key)


def _run_call_errors(store: Store, batch_size: int) -> None:
    """One failed-call mining pass (gr484070): cluster the last 24h of
    rejected-before-work ``tool_calls`` errors and file/bump one gripe
    per cluster, fired from the daily host-agnostic ``call_errors``
    cadence — any live worker can win it, same shape as
    ``nanopub_stale_sweep``. ``batch_size`` is unused — the pass's own
    per-run gripe cap is the throttle. spends=False: pure local SQL +
    gripe writes, no LLM."""
    del batch_size
    from precis.workers.call_errors import run_call_errors_pass

    result = run_call_errors_pass(store)
    log.info(
        "scheduler: call_errors inner result claimed=%d ok=%d failed=%d",
        result.claimed,
        result.ok,
        result.failed,
    )


def _run_gripe_triage(store: Store, batch_size: int) -> None:
    """One gripe triage tick (pipeline v2 slice 3): claim up to 40 open
    gripes, ONE Sonnet-class batch call, write fix:/cluster:/size:/PRIO
    + STATUS:triaged. Dark unless ``service_config`` lights
    ``gripe_triage`` for this host — gated per host on the cadence
    (``service=``, resolved ``prio``) and again inside the pass.
    ``batch_size`` is unused — the pass's own batch cap is the
    throttle. spends=True: the tick bills one model call."""
    del batch_size
    from precis.workers.gripe_triage import run_gripe_triage_pass

    result = run_gripe_triage_pass(store)
    log.info(
        "scheduler: gripe_triage inner result claimed=%d ok=%d failed=%d",
        result.claimed,
        result.ok,
        result.failed,
    )


#: ``anki_sync`` failure backoff (§A). A failed tick holds the *cadence* back
#: ``min(fail_count, _ANKI_MAX_BACKOFF_STEPS) * _ANKI_BACKOFF_STEP_HOURS``
#: before the next attempt — the whole-account analogue of
#: ``orcid_enrich``'s per-node ``meta.fetch_fail_count`` idiom. There is no
#: per-ref row to stamp here (one sync covers an entire AnkiWeb account), so
#: the state lives in two ``app_state`` settings.
#:
#: Why: nothing in ``precis.anki`` handles a 429, a backoff or a retry — an
#: expired password, an AnkiWeb outage or a throttle we cannot even observe
#: previously re-ran at full cadence indefinitely. Backoff is a *cadence*
#: concern on purpose: a human running ``precis anki-sync`` by hand is
#: debugging and must never be told to come back in 30 hours.
_ANKI_BACKOFF_STEP_HOURS = 6
_ANKI_MAX_BACKOFF_STEPS = 5
_ANKI_FAIL_COUNT_KEY = "anki_sync:fail_count"
_ANKI_NEXT_ATTEMPT_KEY = "anki_sync:next_attempt_at"


def _anki_fail_count(store: Store) -> int:
    """Consecutive ``anki_sync`` cadence failures; 0 when unset/unparsable."""
    try:
        return max(0, int(store.get_setting(_ANKI_FAIL_COUNT_KEY) or 0))
    except (TypeError, ValueError):
        return 0


def _anki_backoff_until(store: Store) -> datetime | None:
    """When the next cadence attempt is allowed, or None if unthrottled."""
    from precis.utils.timeutil import as_utc

    raw = store.get_setting(_ANKI_NEXT_ATTEMPT_KEY)
    return as_utc(raw) if raw else None


def _anki_record_failure(store: Store) -> None:
    """Widen the backoff after a failed tick. Best-effort: a failed stamp must
    not mask the sync error the caller is about to re-raise."""
    count = _anki_fail_count(store) + 1
    hours = min(count, _ANKI_MAX_BACKOFF_STEPS) * _ANKI_BACKOFF_STEP_HOURS
    try:
        store.set_setting(_ANKI_FAIL_COUNT_KEY, str(count))
        store.set_setting(
            _ANKI_NEXT_ATTEMPT_KEY,
            (datetime.now(UTC) + timedelta(hours=hours)).isoformat(),
        )
    except Exception:
        log.exception("scheduler: could not record anki_sync backoff")


def _anki_clear_backoff(store: Store) -> None:
    """Drop the backoff after a clean tick. Best-effort, same reasoning."""
    if not _anki_fail_count(store) and not store.get_setting(_ANKI_NEXT_ATTEMPT_KEY):
        return
    try:
        store.set_setting(_ANKI_FAIL_COUNT_KEY, "0")
        store.set_setting(_ANKI_NEXT_ATTEMPT_KEY, "")
    except Exception:
        log.exception("scheduler: could not clear anki_sync backoff")


def _run_anki_sync(store: Store, batch_size: int) -> None:
    """One AnkiWeb sync tick, fired from the melchior-pinned ``anki_sync``
    cadence (§A) instead of the retired standalone 30-min launchd timer.
    Reads the fix/project flags from config (env), matching the plist's
    ``PRECIS_ANKI_FIX_ENABLED`` / ``PRECIS_ANKI_PROJECT_ENABLED``; the
    pg advisory lock in :func:`precis.workers.anki_sync.run_anki_sync` still
    serializes against a concurrent manual ``precis anki-sync`` run.

    Wrapped in the failure backoff above: a throttled tick logs and returns
    without touching AnkiWeb, a failure widens the window and re-raises so
    the generic cadence wrapper still logs it, and a clean tick clears it."""
    from precis.config import load_config
    from precis.workers.anki_sync import run_anki_sync

    until = _anki_backoff_until(store)
    now = datetime.now(UTC)
    if until is not None and now < until:
        log.info(
            "scheduler: anki_sync — backing off after %d consecutive failure(s); "
            "next attempt %s",
            _anki_fail_count(store),
            until.isoformat(),
        )
        return

    cfg = load_config()
    try:
        summary = run_anki_sync(
            store, cfg, fix=cfg.anki_fix_enabled, project=cfg.anki_project_enabled
        )
    except Exception:
        _anki_record_failure(store)
        raise
    _anki_clear_backoff(store)
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
    """One catalog Open API cursor-walk tick (gr264357), fired from
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


def _run_cfp_watch(store: Store, batch_size: int) -> None:
    """Weekly re-check of ``WATCH:*``-tagged cfp refs (gr462088).
    ``batch_size`` is unused — the pass caps its own fetches. See
    workers/cfp_watch.py."""
    from precis.workers.cfp_watch import run_cfp_watch_pass

    result = run_cfp_watch_pass(store)
    log.info(
        "scheduler: cfp_watch inner result claimed=%d ok=%d failed=%d",
        result.claimed,
        result.ok,
        result.failed,
    )


def _run_citer_watch(store: Store, batch_size: int) -> None:
    """Daily OpenAlex cited-by watch (gr513769): poll ``cites:`` for
    works published since the last poll that cite the held, used papers,
    score by how many held papers each cites, mint the top scorers as
    stubs under a daily cap, write a ``kind='memory'`` digest.
    Dark-gated inside the pass: no ``citer_watch`` service_config row
    (prio > 0) → all-zeros no-op, so the cadence's lease advances
    harmlessly while dark (the ``dream_consolidate`` shape — a
    never-claimed lease would read as "stopped firing" to health_digest).
    ``batch_size`` is unused — the pass bounds itself via the seed cap
    and the daily mint cap. spends=False: no LLM — the OpenAlex calls
    draw on the shared ``openalex`` limiter row, not the model budget."""
    del batch_size
    from precis.workers.citer_watch import run_citer_watch_pass

    result = run_citer_watch_pass(store)
    log.info(
        "scheduler: citer_watch inner result claimed=%d ok=%d failed=%d",
        result["claimed"],
        result["ok"],
        result["failed"],
    )


def _run_secret_check(store: Store, batch_size: int) -> None:
    """Weekly credential probe (gr496305 item 4): runs
    ``secret_status.weekly_report`` and raises one ``secret_check`` alert
    per ``failed`` credential. ``batch_size`` is unused — the pass walks
    the whole KNOWN_SECRETS registry (a fixed, bounded set). Probes
    resolve the vault/env on whichever host wins the lease —
    env-override-wins, same as the manual `precis secret check`. See
    workers/secret_check.py."""
    from precis.workers.secret_check import run_secret_check_pass

    result = run_secret_check_pass(store)
    log.info(
        "scheduler: secret_check inner result claimed=%d ok=%d failed=%d",
        result.claimed,
        result.ok,
        result.failed,
    )


def _run_secret_status(store: Store, batch_size: int) -> None:
    """Mint one ``secret_status`` checker job, fired from the periodic
    ``secret_status`` cadence — the job (not the scheduler) runs the
    probes, on whichever ``job_inproc`` worker claims it. ``batch_size``
    is unused — the pending-job dedup in ``mint_status_job`` IS the cap.
    See workers/secret_check.py + job_types/secret_status.py."""
    del batch_size
    from precis.workers.secret_check import mint_status_job

    ref_id = mint_status_job(
        store, origin="background", title="secret_status (periodic)"
    )
    if ref_id is not None:
        log.info("scheduler: minted secret_status job %d", ref_id)


def _run_memory_thread_sweep(store: Store, batch_size: int) -> None:
    """One ``section:threads`` retirement sweep (gr485358 item 1), fired
    from the daily host-agnostic ``memory_thread_sweep`` cadence.
    ``batch_size`` is unused — the pass walks every live thread node each
    fire (a bounded set; prod had 22 on 2026-10-14). See
    workers/memory_thread_sweep.py; no network, no LLM, purely local SQL.
    """
    from precis.workers.memory_thread_sweep import run_memory_thread_sweep_pass

    result = run_memory_thread_sweep_pass(store)
    log.info(
        "scheduler: memory_thread_sweep inner result claimed=%d ok=%d failed=%d",
        result.claimed,
        result.ok,
        result.failed,
    )


def _run_ref_body_bytes(store: Store, batch_size: int) -> None:
    """One incremental ``ref_body_bytes`` recompute (migration 0266),
    fired from the ``ref_body_bytes`` cadence — refs whose body chunks
    changed since the stored watermark, recomputed into the side table
    in small batches. ``batch_size`` is unused — the pass bounds itself
    (BATCH refs per short transaction). See workers/ref_body_bytes.py;
    pure local SQL, no LLM."""
    del batch_size
    from precis.workers.ref_body_bytes import run_ref_body_bytes_pass

    result = run_ref_body_bytes_pass(store)
    log.info(
        "scheduler: ref_body_bytes inner result claimed=%d ok=%d failed=%d",
        result.claimed,
        result.ok,
        result.failed,
    )


def _run_ref_body_bytes_verify(store: Store, batch_size: int) -> None:
    """The daily full-verification sibling of ``ref_body_bytes``: re-walks
    every ref so a pure chunk DELETE (no timestamp records one) still
    lands in the side table. ``batch_size`` is unused — the pass pages
    refs itself. workers/ref_body_bytes.py; pure local SQL, no LLM."""
    del batch_size
    from precis.workers.ref_body_bytes import run_ref_body_bytes_pass

    result = run_ref_body_bytes_pass(store, verify=True)
    log.info(
        "scheduler: ref_body_bytes_verify inner result claimed=%d ok=%d failed=%d",
        result.claimed,
        result.ok,
        result.failed,
    )


def _run_memory_recall(store: Store, batch_size: int) -> None:
    """One P4 recall-set score (``precis.workers.memory_recall``),
    fired from the weekly host-agnostic ``memory_recall`` cadence — any
    live worker can win it, same shape as ``cfp_watch``, except the
    ``eligible`` gate below keeps hosts that cannot build the configured
    embedder from claiming a fire they would only fail. ``batch_size``
    is unused — the pass scores the whole packaged question set each
    fire. See workers/memory_recall.py; SQL + the remote embedder only,
    no LLM."""
    from precis.workers.memory_recall import run_memory_recall_pass

    result = run_memory_recall_pass(store)
    log.info(
        "scheduler: memory_recall inner result claimed=%d ok=%d failed=%d",
        result.claimed,
        result.ok,
        result.failed,
    )


def _run_nanopub_stale_sweep(store: Store, batch_size: int) -> None:
    """One nanopub candidate re-gate sweep (gr279770's sweep half), fired
    from the daily host-agnostic ``nanopub_stale_sweep`` cadence.
    ``batch_size`` is unused — the pass re-gates every live ``candidate``
    row each fire (the same bounded set ``health_digest``'s
    ``staged_candidates_fresh`` check already reads whole). See
    workers/nanopub_stale_sweep.py; no network, no LLM, purely local SQL."""
    from precis.workers.nanopub_stale_sweep import run_nanopub_stale_sweep_pass

    result = run_nanopub_stale_sweep_pass(store)
    log.info(
        "scheduler: nanopub_stale_sweep inner result claimed=%d ok=%d failed=%d",
        result.claimed,
        result.ok,
        result.failed,
    )


def _run_dream_consolidate(store: Store, batch_size: int) -> None:
    """One dream-consolidation sweep (2026-10 dream hygiene): retire
    dreams unrecalled+untouched ≥60d (``thread:`` dreams ≥90d), writing
    one supersedes summary per interest cluster. Host-agnostic, daily.
    Dark-gated inside the pass: no ``dream_consolidate`` service_config
    row (prio > 0) → all-zeros no-op; lit but ``dream_consolidate.dry_run``
    still on (the default) → plans and reports only.
    ``batch_size`` is unused — the pass caps itself via
    ``MAX_CANDIDATES_PER_FIRE``. spends=False: SQL + stored embeddings,
    no LLM."""
    from precis.workers.dream_consolidate import run_dream_consolidate_pass

    result = run_dream_consolidate_pass(store)
    log.info(
        "scheduler: dream_consolidate inner result claimed=%d ok=%d failed=%d",
        result.claimed,
        result.ok,
        result.failed,
    )


def _run_unrecorded_revisions(store: Store, batch_size: int) -> None:
    """One ``(unrecorded)``-revisions count (local-mesh-upkeep §2's
    "nightly count names the write paths to fix"), fired from the daily
    host-agnostic ``revisions_unrecorded`` cadence. ``batch_size`` is
    unused — the pass counts the whole partial-index set each fire.
    See workers/revisions_unrecorded.py; pure local SQL plus one
    ``app_settings`` marker write, no LLM."""
    from precis.workers.revisions_unrecorded import run_unrecorded_revisions_pass

    result = run_unrecorded_revisions_pass(store)
    log.info(
        "scheduler: revisions_unrecorded inner result claimed=%d ok=%d failed=%d",
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


def _claude_agent_lane() -> bool:
    """gr518089: whether THIS host can run a ``call_claude_agent``-class
    cadence — the ``claude`` binary on PATH/``PRECIS_CLAUDE_BIN``, or the
    verified container lane (``PRECIS_AGENT_CONTAINER`` opted in AND its
    runtime+image+auth probe green) — the same rescue the LLM router's
    ``_claude_rung_unavailable`` applies to claude_agent rungs. A host with
    neither skips the claim so a capable host still gets the fire
    (drop-no-fire, same contract as the other ``eligible`` checks above)."""
    from precis.workers import capability_probe

    return capability_probe.claude_agent_capable()


def _gripe_triage_eligible() -> bool:
    """gr518089: strict ``claude_bin_present`` — the triage tick's batch
    call goes through claude_p-class transports that can't containerize,
    so a host without the binary would claim the lease only to pause
    (``cli_unavailable``); it skips instead and a capable host still gets
    the fire."""
    from precis.workers import capability_probe

    return capability_probe.claude_bin_present()


def _structural_eligible() -> bool:
    """The SAME gate ``run_structural_pass`` checks internally
    (``precis.workers.review._gate_enabled``) AND the claude-agent lane
    probe (gr518089) — a host without ``PRECIS_STRUCTURAL_REVIEW`` set, or
    with no runnable claude lane, skips *before* the claim, so the lease is
    never advanced by a host that can't do the work (drop-no-fire, not a
    stolen fire — a later eligible host still gets it)."""
    from precis.workers.review import _gate_enabled
    from precis.workers.structural import STRUCTURAL

    return _gate_enabled(STRUCTURAL.gate_env) and _claude_agent_lane()


def _deep_review_eligible() -> bool:
    """Same shape as ``_structural_eligible`` — reuses the review driver's
    gate helper against ``PRECIS_DEEP_REVIEW``, plus the same claude-agent
    lane probe (gr518089)."""
    from precis.workers.deep_review import DEEP_REVIEW
    from precis.workers.review import _gate_enabled as _review_gate_enabled

    return _review_gate_enabled(DEEP_REVIEW.gate_env) and _claude_agent_lane()


def _dream_agent_eligible() -> bool:
    from precis.workers.dream_agent import eligible

    return eligible() and _claude_agent_lane()


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


def _memory_recall_eligible() -> bool:
    """Only a host whose config can build a recall embedder may win the
    ``memory_recall`` lease — a ``remote``-embedder host without
    ``PRECIS_EMBEDDER_URL`` would claim the fire and then fail it
    (:class:`~precis.embedder.EmbedderNotConfigured`), so it skips before
    the claim and leaves the fire for a capable host."""
    from precis.config import load_config

    cfg = load_config()
    return cfg.embedder != "remote" or bool(cfg.embedder_url)


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
    # docs/backlog/gripe-pipeline-v2.md §Intake: the duplicate-parent
    # backfill. Same 300 s as materialize — a filed gripe's embedding
    # lands inside that pass's window, so this rides the same latency
    # band the spec budgets (~5 min busy). Host-agnostic; spends=False
    # (one ANN query per gripe, zero model/embedder calls).
    Cadence(name="gripe_parents", interval_s=300, run=_run_gripe_parents),
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
    # Once a day (Reto's call 2026-09-26), not the old 30 minutes. AnkiWeb is
    # someone else's server and the old cadence bought nothing: 48 fires/day
    # per account, each one a fresh ``sync_login`` with the password plus two
    # ``sync_collection`` round-trips, against a card set that changes a
    # handful of times a week. Off the exact day (24h29m) so it doesn't pile
    # onto the same tick as the other daily cadences below.
    Cadence(
        name="anki_sync",
        interval_s=24 * 3600 + 29 * 60,
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
    # gr279770's sweep half: re-gate every live `candidate` row against the
    # current mint rules and discard the ones that no longer pass (a lint
    # code landed since staging, or a live dispute arrived) — the filter
    # half (54d47e8c) only badges them on the /nanopub queue, it never
    # took a row out of candidacy, so health_digest's
    # `staged_candidates_fresh` watchdog kept re-firing. Host-agnostic,
    # daily, off the exact day boundary like the other three nanopub/
    # catalog sweeps above so their fires don't land together. No
    # `eligible` check: pure local SQL, nothing to gate on. spends=False:
    # no LLM anywhere in the re-gate.
    Cadence(
        name="nanopub_stale_sweep",
        interval_s=24 * 3600 + 41 * 60,
        run=_run_nanopub_stale_sweep,
    ),
    # gr462088: weekly re-fetch of WATCH:*-tagged cfp refs; a changed page
    # or deadline raises a cfp_watch alert. Host-agnostic; off the exact
    # week (7d+53m). Bounded to MAX_FETCHES_PER_RUN fetches. spends=False.
    Cadence(
        name="cfp_watch",
        interval_s=7 * 24 * 3600 + 53 * 60,
        run=_run_cfp_watch,
    ),
    # The graph gardener's cadence is NOT a scheduler cadence — it is an
    # operator-seeded recurring todo (meta.schedule +
    # executor='job_inproc' + job_type='graph_garden'; the tick spawns a
    # subtask the dispatch pass mints the job from). The service_config
    # on/off gate and the dry-run cooling moved into the job's per-run
    # dispatch (precis.graph.garden.service).
    # gr484070: mine the tool_calls ledger for rejected-before-work call
    # clusters and file/bump one gripe each.
    # Host-agnostic, daily, off the exact day
    # boundary (+47m) like the other daily sweeps. No `eligible` check:
    # pure local SQL + gripe writes, nothing to gate on. The pass's own
    # per-run cap (MAX_NEW_GRIPES) is the flood control. spends=False.
    Cadence(
        name="call_errors",
        interval_s=24 * 3600 + 47 * 60,
        run=_run_call_errors,
    ),
    # Gripe pipeline v2 slice 3: the LLM triage tick — 15 minutes, one
    # Sonnet-class batch call per fire. `service` gates the claim on the
    # host's resolved gripe_triage prio (gr518089: an exact-host `prio 0`
    # row dims a fleet-wide `*` light — a dimmed host skips the claim so
    # a lit one still gets the fire); the pass re-checks the same gate
    # inside. `eligible` is the strict claude-binary probe (gr518089: a
    # host with no `claude` would claim only to pause `cli_unavailable` —
    # claude_p can't containerize, so no lane rescue here). spends=True:
    # the tick bills the model call, so the daily ceiling gates it.
    Cadence(
        name="gripe_triage",
        interval_s=15 * 60,
        run=_run_gripe_triage,
        eligible=_gripe_triage_eligible,
        service="gripe_triage",
        spends=True,
    ),
    # gr485358 item 1: retire dead section:threads memory nodes — 'Left:
    # none' first lines, nodes another live memory supersedes, and nodes
    # untouched ≥14 days (minting a carry-over todo first when the Left:
    # line names open work). Host-agnostic, daily, off the exact day
    # boundary like the other daily sweeps. No `eligible` check: pure
    # local SQL. spends=False: no LLM anywhere in the sweep.
    Cadence(
        name="memory_thread_sweep",
        interval_s=24 * 3600 + 43 * 60,
        run=_run_memory_thread_sweep,
    ),
    # gr513769: the OpenAlex cited-by watch — daily, host-agnostic, off
    # the exact day boundary (+57m, clear of the other daily fires).
    # Ships DISABLED: the cadence fires and the lease advances, but the
    # pass no-ops until a `service_config` row lights `citer_watch`
    # (prio > 0) — `precis service prio '*' citer_watch 1` is the prod
    # flag. No `service=` gate on the cadence on purpose: that gate
    # leaves the lease unclaimed while dark, which health_digest's
    # cadence-staleness check reports as "stopped firing" forever.
    # spends=False: no LLM; the OpenAlex calls draw the shared limiter.
    Cadence(
        name="citer_watch",
        interval_s=24 * 3600 + 57 * 60,
        run=_run_citer_watch,
    ),
    # 2026-10 dream hygiene: consolidate stale dreams — retire unrecalled+
    # untouched dreams (60d; 90d for thread: memories) behind one supersedes
    # summary per interest cluster. Host-agnostic, daily, off the exact day
    # boundary (+73m) like the other sweeps. No `eligible` check: pure local
    # SQL + stored embeddings — the gate is inside the pass (dark until a
    # `dream_consolidate` service_config row lights it, then report-only
    # until `dream_consolidate.dry_run` is set off). spends=False: no LLM
    # anywhere.
    Cadence(
        name="dream_consolidate",
        interval_s=24 * 3600 + 73 * 60,
        run=_run_dream_consolidate,
    ),
    # The P4 scorer (workers/memory_recall.py) made durable: score the P4
    # recall question set weekly, record the k=5 score (native/mirror
    # split) in app_settings, and raise a WARN alert when it drops below
    # the last recorded run. Host-agnostic, off the exact week like
    # cfp_watch (+17m so their fires don't land together). `eligible`
    # keeps embedder-less hosts from claiming a fire they'd only fail.
    # spends=False: the embedder HTTP call is local, no LLM.
    Cadence(
        name="memory_recall",
        interval_s=7 * 24 * 3600 + 17 * 60,
        run=_run_memory_recall,
        eligible=_memory_recall_eligible,
    ),
    # gr496305 item 4: the weekly credential check — run the /secrets
    # probes on a cadence and raise one secret_check alert per `failed`
    # credential, so a revoked key doesn't wait for a /whatneedsdoing
    # session. Host-agnostic like cfp_watch; off the exact week (+29m so
    # the weekly fires don't land together). No `eligible` gate: a host
    # with nothing configured reports every row missing — quiet, no
    # alerts. spends=False: fixed-endpoint HTTP probes only, no LLM.
    Cadence(
        name="secret_check",
        interval_s=7 * 24 * 3600 + 29 * 60,
        run=_run_secret_check,
    ),
    # The /secrets stored-status refresher: mints one `secret_status`
    # checker job per fire (the page itself never probes — it reads the
    # app_state blob the job rewrites). ~4h keeps "checked <age> ago"
    # honest for ~13 cheap auth-only calls; off the exact 4h (+19m) so it
    # doesn't pile onto the draft_refresh_scan tick. Host-agnostic; no
    # `eligible` gate — a host with nothing configured records all-missing
    # quietly. spends=False: fixed-endpoint HTTP probes, no LLM.
    Cadence(
        name="secret_status",
        interval_s=4 * 3600 + 19 * 60,
        run=_run_secret_status,
    ),
    # local-mesh-upkeep §2's nightly count: how many `revisions` rows
    # landed with reason='(unrecorded)', grouped by the write path
    # (target kind, event, actor, covered subject) so the unwrapped
    # writers stay named until they're wrapped. Host-agnostic, daily,
    # off the exact day boundary like the other daily sweeps. No
    # `eligible` check: pure local SQL + one app_settings marker write.
    # spends=False: no LLM anywhere in the count.
    Cadence(
        name="revisions_unrecorded",
        interval_s=24 * 3600 + 67 * 60,
        run=_run_unrecorded_revisions,
    ),
    # Migration 0266's async half: keep the ref_body_bytes side table
    # current without a chunk write trigger (writers must not pay — see
    # workers/ref_body_bytes.py). Two cadences: the 15-minute
    # incremental (chunks created_at/retired_at past a stored
    # watermark) and the daily full verify (+89m, clear of the other
    # daily fires) that catches pure deletes. Host-agnostic; pure local
    # SQL, no `eligible` check. spends=False.
    Cadence(
        name="ref_body_bytes",
        interval_s=15 * 60,
        run=_run_ref_body_bytes,
    ),
    Cadence(
        name="ref_body_bytes_verify",
        interval_s=24 * 3600 + 89 * 60,
        run=_run_ref_body_bytes_verify,
    ),
)


#: Overall bound on one cadence's work inside a fire (gr486823). Cadence
#: work is arbitrary blocking I/O (watch_poll→S2, anki_sync→AnkiWeb,
#: ots_sweep→calendar, …) and this pass runs them inline on the shared
#: worker rotation — a wedged ``cad.run`` starves every other cadence the
#: same way the wedged orcid_enrich ref-pass did. Past the bound the run
#: is *abandoned* — it keeps going detached on its daemon thread — and
#: while it is still alive the cadence's claim is skipped entirely, so
#: the lease goes stale and health_digest's cadence-staleness check
#: reports the truth instead of advancing ``last_fired_at`` over work
#: that never landed. Default sits under the staleness margin floor
#: (health_digest's ``_CADENCE_MARGIN_FLOOR_S`` = 900) so a single wedge
#: resolves before the watchdog would even alert. A detached run sharing
#: ``store`` is safe for the same reason as a detached ref-pass —
#: ``Store`` ops each check out their own pooled conn, and the pinned-conn
#: ``ContextVar`` does not propagate across threads (see run_loop's
#: ref-pass deadline docstring for the full argument + the test that
#: proves it).
_INFLIGHT: dict[str, threading.Thread] = {}
_RUN_DEADLINE_ENV = "PRECIS_CADENCE_RUN_DEADLINE_S"
_RUN_DEADLINE_DEFAULT_S = 300.0


#: In-process mutual exclusion for the whole pass: the dedicated thread and
#: the in-rotation backstop would otherwise run the same cadence
#: concurrently when a run outlives its interval, and race ``_INFLIGHT``.
#: Taken non-blocking — the loser skips this tick (the winner is already
#: doing the work), never queues.
_PASS_LOCK = threading.Lock()


def run_scheduler_pass(
    store: Any,
    *,
    host: str,
    batch_size: int = 32,
    cadences: tuple[Cadence, ...] = CADENCES,
    run_deadline_s: float | None = None,
) -> BatchResult:
    """Claim + fire every due cadence this cycle, unless another thread of
    this process is already inside the pass (non-blocking lock; the skip
    returns an empty result and logs at debug). See ``_scheduler_pass``."""
    if not _PASS_LOCK.acquire(blocking=False):
        log.debug("scheduler: pass already running in this process; skipping")
        return BatchResult(handler="scheduler", claimed=0, ok=0, failed=0)
    try:
        return _scheduler_pass(
            store,
            host=host,
            batch_size=batch_size,
            cadences=cadences,
            run_deadline_s=run_deadline_s,
        )
    finally:
        _PASS_LOCK.release()


def _scheduler_pass(
    # forwards into the Store-typed _run_*/_over_daily_budget helpers;
    # test_scheduler_budget_gate.py calls this directly with a minimal
    # _Store (only claim_scheduler_lease) narrower than Store.
    store: Any,
    *,
    host: str,
    batch_size: int = 32,
    cadences: tuple[Cadence, ...] = CADENCES,
    run_deadline_s: float | None = None,
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

    ``run_deadline_s`` bounds each cadence's work (default
    :data:`_RUN_DEADLINE_DEFAULT_S`, env ``PRECIS_CADENCE_RUN_DEADLINE_S``;
    <= 0 disables). A cadence whose previous fire is still running
    detached past that bound is skipped BEFORE the claim — no lease
    advance, so staleness alerting reports it honestly.
    """
    bound = (
        run_deadline_s
        if run_deadline_s is not None
        else env_float(_RUN_DEADLINE_ENV, _RUN_DEADLINE_DEFAULT_S, lo=0.0)
    )
    claimed = ok = failed = 0
    svc_resolver: ServiceConfigResolver | None = None

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
        if cad.service is not None:
            # Per-host service_config gate (dark until lit): a host whose
            # resolved prio is 0 skips WITHOUT claiming — the lease stays
            # due so a lit host can still take this fire.
            if svc_resolver is None:
                from precis.workers.service_config import ServiceConfigResolver

                svc_resolver = ServiceConfigResolver(store, host)
            if not svc_resolver.enabled(cad.service, default_on=False):
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
        prev = _INFLIGHT.get(cad.name)
        if prev is not None:
            if prev.is_alive():
                # A fire abandoned past its bound is still running — skip
                # the claim (no lease advance) so staleness alerting reads
                # the wedge truthfully and we never stack a second run.
                log.warning(
                    "scheduler: cadence %s still running detached past the "
                    "%gs run bound — skipping claim",
                    cad.name,
                    bound,
                )
                continue
            _INFLIGHT.pop(cad.name, None)
            log.info("scheduler: cadence %s detached run finished", cad.name)
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
            if bound > 0:
                call_with_deadline(
                    partial(cad.run, store, batch_size),
                    deadline_s=bound,
                    label=f"cadence {cad.name}",
                )
            else:
                cad.run(store, batch_size)
            ok += 1
        except DeadlineExceeded as exc:
            _INFLIGHT[cad.name] = exc.thread
            log.error(
                "scheduler: cadence %s exceeded %gs run bound on %s; abandoned "
                "(still running detached)",
                cad.name,
                bound,
                host,
            )
            failed += 1
        except Exception:
            log.exception("scheduler: cadence %s work raised", cad.name)
            failed += 1

    return BatchResult(handler="scheduler", claimed=claimed, ok=ok, failed=failed)


#: How long the thread sleeps between attempts. A module global (not a local
#: constant) so tests can monkeypatch it down for a fast-running loop — same
#: convention as heartbeat.py's ``_THREAD_TICK_SECONDS``.
_SCHEDULER_TICK_SECONDS = 5.0


def _sleep_with_stop_check(duration: float, should_stop: Callable[[], bool]) -> None:
    """Sleep ``duration`` seconds in ~1s increments, returning early the
    moment ``should_stop()`` flips — so a shutdown request is noticed
    promptly rather than sleeping through it. Local copy of heartbeat.py's
    helper (kept private there on purpose, same as this one)."""
    remaining = duration
    while remaining > 0 and not should_stop():
        step = min(1.0, remaining)
        time.sleep(step)
        remaining -= step


def _scheduler_thread_body(
    dsn: str,
    host: str,
    batch_size: int,
    gate: Callable[[str], bool] | None,
    cadences: tuple[Cadence, ...],
    should_stop: Callable[[], bool],
) -> None:
    """The ``start_scheduler_thread`` loop body — see that function's
    docstring for the design rationale."""
    from precis.store import Store

    store: Store | None = None
    while not should_stop():
        try:
            if store is None:
                try:
                    store = Store.connect(dsn)
                except Exception:
                    log.warning(
                        "scheduler thread: failed to connect; retrying",
                        exc_info=True,
                    )
                    store = None
            if store is not None and (gate is None or gate("scheduler")):
                run_scheduler_pass(
                    store,
                    host=host,
                    batch_size=batch_size,
                    cadences=cadences,
                )
        except Exception:
            # Belt-and-suspenders: a bug in this loop must never take the
            # whole worker down with it. The store is dropped too — its
            # connection may be the fault — and reconnected on the next tick.
            log.warning("scheduler thread: tick failed", exc_info=True)
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass
                store = None

        _sleep_with_stop_check(_SCHEDULER_TICK_SECONDS, should_stop)

    if store is not None:
        try:
            store.close()
        except Exception:
            pass


def start_scheduler_thread(
    dsn: str,
    *,
    host: str,
    batch_size: int = 32,
    gate: Callable[[str], bool] | None = None,
    cadences: tuple[Cadence, ...] = CADENCES,
    should_stop: Callable[[], bool] | None = None,
) -> threading.Thread:
    """Start a dedicated daemon thread that fires due cadences independently
    of the serial worker rotation (gr518349).

    ``workers/runner.py``'s ``run_loop`` runs handlers then ref_passes
    strictly serially — a single long pass (observed: hub_refine holding the
    rotation ~10 min on the agent host) starves the in-rotation ``scheduler`` pass,
    so cadences whose only lit/eligible host is that worker stop firing
    (gripe_triage, deep_review, cron_tick). This thread bounds cadence
    staleness to roughly ``_SCHEDULER_TICK_SECONDS`` regardless of how long
    any single rotation pass runs.

    Owns a SEPARATE ``Store.connect(dsn)`` from the main loop's store —
    psycopg connections aren't usable concurrently across threads. Calls the
    same :func:`run_scheduler_pass` the in-rotation pass calls; lease claims
    are atomic conditional advances, so a thread/rotation race is a harmless
    ``claimed=0`` for the loser, never a double fire — the in-rotation
    ``_scheduler_pass`` stays registered as a backstop (same argument as the
    heartbeat thread vs its in-rotation pass).

    ``gate`` is the worker's per-cycle service gate (``cli/worker.py``'s
    ``_pass_gate``): consulted once per tick as ``gate("scheduler")`` so a
    ``service_config`` flip dims the thread within one cache TTL, the same
    dimming the in-rotation pass gets. ``None`` (``--only``) means ungated.

    The thread never crashes the worker: a connect failure or a pass
    exception is caught and logged, the store is dropped, and the loop
    reconnects on the next tick.

    Returns the started (daemon, so it won't block process exit) thread;
    the caller usually just fires-and-forgets it.
    """
    stop = should_stop or (lambda: False)
    thread = threading.Thread(
        target=_scheduler_thread_body,
        args=(dsn, host, batch_size, gate, cadences, stop),
        name="scheduler",
        daemon=True,
    )
    thread.start()
    return thread


__all__ = ["CADENCES", "Cadence", "run_scheduler_pass", "start_scheduler_thread"]

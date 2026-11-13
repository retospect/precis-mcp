"""Footprint admission + model affinity on the pull (gr595542).

The claim path (:mod:`precis.workers.executors._common`) reserves counted
tokens (``gpu``, ``llm:<model>``, …) today, with no notion of whether the
job's predicted footprint fits the host's headroom, and no preference for
the model a GPU host last loaded. This module is that missing policy,
keeping the gripe's scope — the discrete GPU/CPU science lane, not LLM
serving (``llm-lane-scheduler.md``, which stays persistent):

* :func:`admission_units` — extra ``resource_slots`` units a claim must
  reserve beyond its counted tokens. GPU: a job whose *effective* model
  (:func:`effective_model` — an explicit ``meta.params.model``, else the
  job_type's declared default; ``struct_relax`` omits ``params.model`` and
  loads ``mace_mp``) has a ``model_footprint`` row (0322) reserves
  ``vram_mb`` = footprint + margin, provided the host advertises both the
  ``vram_mb`` row and the per-card ``vram_card_mb`` ceiling. CPU: a job on
  the science executor (``ssh_node``) with enough measured runs in the
  ``(job_type, meta.size)`` rollup reserves ``mem_mb`` = p95 × margin.
  Either side missing (unlearned model, unadvertised capacity row, too few
  samples) yields no units: admission is dark until the data exists,
  today's token-only claim unchanged.
* :func:`admission_block` — the pre-reserve veto the review added on top
  (td655172), checked per claim round against the *live* host state the
  static ledger can't see:

  - **Per-card fit.** The ``vram_mb`` row is host-wide, so a 40 GiB model
    "fits" two 24 GiB cards in the ledger while no single card can hold
    it. The veto refuses the claim when footprint + margin exceeds the
    host's ``vram_card_mb`` (largest single card).
  - **Live headroom.** The capacity rows are static totals minus reserves;
    workloads that never reserved through the ledger (LLM serving,
    interactive use) consume real RAM/VRAM without shrinking them, so the
    conditional decrement alone over-admits. The veto refuses when the
    host's latest measured *admittable* free — ``host_heartbeat.meta``'s
    ``vram_free_mb`` (the best *single card's* free, never the cross-card
    sum) / ``mem_free_mb``, written per beat by
    :mod:`precis.workers.heartbeat` — less the units earlier claims
    still hold, can't cover the new
    units. "Admittable": the probe nets the measured host's own reserve
    (``PRECIS_HOST_MEM_RESERVE_MB`` / ``PRECIS_HOST_VRAM_RESERVE_MB``),
    the same subtraction the static capacity row carries — netting it at
    measurement keeps the reserve the *measured* host's, not the
    claimer's, and an untracked consumer eating into the reserve vetoes
    admissions instead of silently borrowing it (td655171 review). A
    held reservation counts in *full* until the reservation itself
    releases (``meta.reserved`` is popped at refund): the measured free
    is aggregate, so no drop below the claim-time level can be
    attributed to the reserved job over an untracked consumer —
    releasing on drops could free a hold whose job had not allocated
    and over-admit, and no job-attributable measurement exists yet
    (the run's own ``meta.footprint`` lands at terminalization, when
    the reservation releases anyway). The residual is *under*-admission
    by at most the in-flight reservations — a materialized job's units
    still count while the beat already shows its consumption — which
    is the safe direction (td655171 review; replaces the td655172
    drop-evidenced release, and the beat+window timer before it).
    Missing measurement reads as dark (ledger only), same discipline as
    the rest of the module.
  - **Card assignment.** When the beat carries the per-card list
    (``vram_free_cards_mb``) *and* its companion ``vram_reserve_mb``
    stamp, the veto becomes a *pick*: the claim admits
    onto one card whose effective free (measured free less bound pending
    holds, less every unbound hold's units — an unbound job could be on
    any card — less the reserve the stamp carries,
    which the raw per-card list does NOT net the way the scalar probe
    does — td655172 review) covers the units and whose held-job count
    stays under
    :func:`gpu_card_max_jobs` (the "1–2 concurrent jobs per card" of
    cluster-scheduling.md Pillar 3b). The stamp is the measured host's
    own ``PRECIS_HOST_VRAM_RESERVE_MB``, written on its beat for exactly
    this read: the reservation host can be remote (``target_node``), so
    the claimer's own env would subtract the WRONG host's reserve — a
    reserve-0 claimer would admit against a reserving host's raw free,
    and the picked path skips the reserve-netted scalar check that would
    have caught it. The picked index is stamped
    ``meta.reserved.gpu_card`` so the dispatch binds execution to the
    admitted card — ``struct_relax`` passes it as
    ``--gpus device=<i>``/``--device nvidia.com/gpu=<i>`` on the container
    rung and as an explicit ``cuda:<i>`` device to the in-process MLIP
    calculator. No card list, or a list
    without the reserve stamp (a beat predating it) ⇒
    the scalar check above stands and the claim runs unbound — never a
    claimer-env guess at the host's reserve.

* :func:`note_model_use` / :func:`last_models` — the model-affinity
  pair, keyed on :func:`effective_model` so an implicit default steers the
  queue exactly like an explicit one. A successful claim whose job names
  a model (explicitly or by default) stamps ``claim.last_model.<host>``
  (+ ``_at``, epoch seconds) for the host the model loads on (the
  reservation host — ``target_node`` else the claimer); the claim re-rank
  then prefers queued jobs naming that model, as a tiebreak after aged
  prio and before queue age, so it never overrides class/prio fairness.
  The stamp expires after :data:`DEFAULT_MODEL_AFFINITY_TTL_MIN` — the
  soft half of "keep weights resident between tasks": the preference
  decays rather than pinning the host. Actual resident weights with
  teardown on switch or idle timeout remains optional and unbuilt;
  sessions stay per task.

Greedy first-fit per machine needs no code: the claim already walks its
ordered candidates and skips what doesn't fit; this module only makes
"fit" real. Cross-machine bin-packing stays unbuilt by ruling.

Not to be confused with :mod:`precis.workers.admission` — the gr647996
S1 ``admit()``/``release()`` primitive over the same ledger. These units
ride the claim path's own reserve (no TTL hold rows): a job's
reservation is refunded at every leave/terminal transition through
``meta.reserved``, and the S3 rework of the claim path onto ``admit()``
(with hold renewal) supersedes this wiring, not the policy.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from collections.abc import Collection, MutableMapping
from typing import Any

from psycopg import Connection

from precis.store._jobs_ops import model_footprint_vram_mb, predicted_rss_mb
from precis.workers.admission import MEM_MB, VRAM_CARD_MB, VRAM_MB

log = logging.getLogger(__name__)

#: The executor whose claims take CPU (RSS) footprint admission — the
#: discrete GPU/CPU science lane (``ssh_node``: struct_*, pathway
#: compute, fold, retrosynth, …). GPU VRAM admission binds to the
#: ``gpu`` token itself and so is executor-agnostic. Broadening RSS
#: admission to the other lanes is gr647996 S3's claim-path rework.
SCIENCE_EXECUTOR = "ssh_node"

#: Samples a ``(job_type, meta.size)`` key needs before its p95 admits
#: claims — below this the key is too thin to trust and the claim
#: carries no memory reservation (cold start runs nearly alone by the
#: token counts alone, same as today). Override
#: ``PRECIS_FOOTPRINT_MIN_SAMPLES``.
DEFAULT_MIN_SAMPLES = 5
MIN_SAMPLES_ENV = "PRECIS_FOOTPRINT_MIN_SAMPLES"

#: Multiplier on the measured p95 peak RSS the memory reservation
#: covers — the "high percentile plus margin for noisy types" of
#: cluster-scheduling.md Pillar 3b. Override ``PRECIS_RSS_MARGIN_FACTOR``.
DEFAULT_RSS_MARGIN_FACTOR = 1.25
RSS_MARGIN_ENV = "PRECIS_RSS_MARGIN_FACTOR"

#: VRAM headroom kept free on top of a model's footprint — "free VRAM −
#: margin ≥ footprint". Override ``PRECIS_VRAM_MARGIN_MB``.
DEFAULT_VRAM_MARGIN_MB = 1024
VRAM_MARGIN_ENV = "PRECIS_VRAM_MARGIN_MB"

#: How long a host's last-used model stamp steers the claim re-rank —
#: past this the weights read as cold and the preference decays (the
#: idle-timeout half of optional resident weights). Override
#: ``PRECIS_MODEL_AFFINITY_TTL_MIN``.
DEFAULT_MODEL_AFFINITY_TTL_MIN = 30.0
MODEL_AFFINITY_TTL_ENV = "PRECIS_MODEL_AFFINITY_TTL_MIN"

#: Concurrent jobs admitted onto one GPU card — the "at most 1–2
#: concurrent jobs per card" of cluster-scheduling.md Pillar 3b. Bound
#: holds count against their own card; a hold with no ``gpu_card``
#: (claimed before binding existed, or while the per-card probe was dark)
#: counts against every card — it could be on the one under test.
#: Override ``PRECIS_GPU_CARD_MAX_JOBS``.
DEFAULT_GPU_CARD_MAX_JOBS = 2
GPU_CARD_MAX_JOBS_ENV = "PRECIS_GPU_CARD_MAX_JOBS"

_LAST_MODEL_KEY_PREFIX = "claim.last_model."
_LAST_MODEL_AT_KEY_PREFIX = "claim.last_model_at."

#: ``host_heartbeat.meta`` keys carrying the latest measured free headroom
#: (MiB) — written per beat by ``workers/heartbeat._collect_and_upsert``
#: and read by :func:`admission_block`. Absent = unmeasurable → dark.
_LIVE_FREE_META_KEYS = {VRAM_MB: "vram_free_mb", MEM_MB: "mem_free_mb"}

#: The per-card companion of ``vram_free_mb``: a JSON int list in
#: ``nvidia-smi`` index order. Present ⇒ card-bound admission; absent ⇒
#: the unbound scalar fallback.
_VRAM_CARDS_META_KEY = "vram_free_cards_mb"

#: The measured host's ``PRECIS_HOST_VRAM_RESERVE_MB``, stamped by the
#: heartbeat alongside the (raw, un-netted) per-card list so the pick
#: subtracts the RESERVATION host's reserve — never the claimer's env,
#: which differs under a remote ``target_node`` (td655172 review).
#: Absent ⇒ the list alone can't drive a pick; the unbound scalar
#: fallback (reserve-netted at measurement) stands.
_VRAM_RESERVE_META_KEY = "vram_reserve_mb"

_UPSERT_APP_STATE = (
    "INSERT INTO app_state (key, value) VALUES (%s, %s) "
    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
    "updated_at = now()"
)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning(
            "footprint admission: %s=%r is not a number; using %g", name, raw, default
        )
        return default


def min_samples() -> int:
    """The minimum rollup sample count for RSS admission."""
    return max(1, int(_env_float(MIN_SAMPLES_ENV, float(DEFAULT_MIN_SAMPLES))))


def rss_margin_factor() -> float:
    """The p95 → reservation multiplier for RSS admission."""
    return max(1.0, _env_float(RSS_MARGIN_ENV, DEFAULT_RSS_MARGIN_FACTOR))


def vram_margin_mb() -> int:
    """The headroom kept free on top of a model's VRAM footprint."""
    return max(0, int(_env_float(VRAM_MARGIN_ENV, float(DEFAULT_VRAM_MARGIN_MB))))


def model_affinity_ttl_s() -> float:
    """The last-model stamp's steering lifetime in seconds."""
    return (
        max(0.0, _env_float(MODEL_AFFINITY_TTL_ENV, DEFAULT_MODEL_AFFINITY_TTL_MIN))
        * 60.0
    )


def gpu_card_max_jobs() -> int:
    """The concurrency cap one GPU card admits."""
    return max(
        1, int(_env_float(GPU_CARD_MAX_JOBS_ENV, float(DEFAULT_GPU_CARD_MAX_JOBS)))
    )


def effective_model(meta: dict[str, Any]) -> str | None:
    """The model a job will actually load: its explicit ``params.model``,
    else its job_type's declared ``default_model``.

    ``struct_relax`` is the case that matters (td655172): its dispatch
    falls back to ``params.get("model") or "mace_mp"``, so a job minted
    without ``params.model`` loads the default — and used to bypass both
    VRAM admission and model affinity even with a learned footprint for
    that model. Resolving through the spec keeps the admission/affinity
    key identical to what the run loads.

    The registry import is function-level on purpose: some job_type
    modules import the executors package, so a module-level import here
    would cycle (same discipline as ``executors/_common``).
    """
    model = (meta.get("params") or {}).get("model")
    if model:
        return str(model)
    job_type = meta.get("job_type")
    if not job_type:
        return None
    from precis.workers.job_types import get_job_type

    spec = get_job_type(str(job_type))
    default = spec.default_model if spec is not None else None
    return str(default) if default else None


def _host_hard_capacity(
    conn: Connection,
    host: str,
    resource: str,
    cache: MutableMapping[tuple[Any, ...], Any],
) -> int | None:
    """The capacity of one hard ``resource_slots`` row on ``host`` (or
    ``None`` when the row is absent), cached per claim pass."""
    key = ("capacity", host, resource)
    if key not in cache:
        row = conn.execute(
            "SELECT capacity FROM resource_slots "
            "WHERE host = %s AND resource = %s AND kind = 'hard'",
            (host, resource),
        ).fetchone()
        cache[key] = int(row[0]) if row is not None else None
    return cache[key]


def _clamp_to_capacity(
    conn: Connection,
    host: str,
    resource: str,
    units: int,
    cache: MutableMapping[tuple[Any, ...], Any],
) -> int:
    """``units`` capped at the host's advertised capacity for ``resource``.

    The measured footprint plus margin can exceed what the host
    advertises (total less reserve) — a p95 × 1.25 on a small box, a
    model + margin over the netted VRAM. ``reserve_resource_slots``
    could then never satisfy it and the claim would return nothing every
    cycle, a permanently unclaimable job. Capped, the job still runs
    alone on an otherwise idle host (the decrement succeeds only at
    full free, so it can't co-run with another reservation). Capacity
    unknown (no hard row, or a zero one) keeps the unclamped units: the
    decrement then refuses exactly as before.
    """
    cap = _host_hard_capacity(conn, host, resource, cache)
    if cap is None or cap <= 0:
        return units
    return min(units, cap)


def _live_free_mb(
    conn: Connection,
    host: str,
    cache: MutableMapping[tuple[Any, ...], Any],
) -> tuple[dict[str, int], list[int] | None, float, int | None]:
    """The host's latest measured free headroom per footprint resource,
    from ``host_heartbeat.meta`` (written per beat by the heartbeat), the
    per-card VRAM list when the beat carries it, the beat's epoch
    seconds, and the ``vram_reserve_mb`` stamp the pick nets off the raw
    list (the measured host's own reserve — td655172 review).
    Absent/unparseable keys read as dark — the caller then
    relies on the ledger alone."""
    key = ("live_free", host)
    if key not in cache:
        row = conn.execute(
            "SELECT meta->>%s, meta->>%s, meta->>%s, meta->>%s, "
            "extract(epoch FROM ts) "
            "FROM host_heartbeat WHERE host = %s",
            (
                _LIVE_FREE_META_KEYS[VRAM_MB],
                _LIVE_FREE_META_KEYS[MEM_MB],
                _VRAM_CARDS_META_KEY,
                _VRAM_RESERVE_META_KEY,
                host,
            ),
        ).fetchone()
        out: dict[str, int] = {}
        cards: list[int] | None = None
        reserve: int | None = None
        beat = 0.0
        if row is not None:
            beat = float(row[4] or 0.0)
            for resource, raw in zip((VRAM_MB, MEM_MB), row[:2], strict=True):
                if raw is None:
                    continue
                try:
                    out[resource] = max(0, int(str(raw)))
                except ValueError:
                    continue  # unparsable stamp reads as dark
            if row[2] is not None:
                try:
                    parsed = json.loads(str(row[2]))
                    cards = [max(0, int(v)) for v in parsed] or None
                except (ValueError, TypeError):
                    cards = None  # unparsable card list reads as unbound
            if row[3] is not None:
                try:
                    reserve = max(0, int(str(row[3])))
                except ValueError:
                    reserve = None  # unparsable stamp reads as absent
        cache[key] = (out, cards, beat, reserve)
    return cache[key]


def _card_index(raw: Any) -> int | None:
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None  # an unparseable stamp reads as unbound


def _hold_rows(conn: Connection, host: str) -> list[tuple[int | None, int]]:
    """Every still-held reservation on ``host`` that occupies a GPU card
    (a ``gpu`` token or ``vram_mb`` units — the claim sits on *some*
    card): ``(gpu_card, vram_mb_units)``. Each counts in full until its
    reservation releases — the beat's aggregate free can't attribute a
    drop to this job over an untracked consumer, so a measured drop
    never releases a hold (td655171 review).
    """
    rows = conn.execute(
        "SELECT meta->'reserved'->>'gpu_card', "
        "COALESCE((meta->'reserved'->'slots'->>%s)::bigint, 0) "
        "FROM refs WHERE kind = 'job' "
        "AND meta->'reserved'->>'host' = %s "
        "AND (meta->'reserved'->'slots' ? 'gpu' "
        "     OR meta->'reserved'->'slots' ? %s)",
        (VRAM_MB, host, VRAM_MB),
    ).fetchall()
    return [
        (_card_index(card_raw), int(units_raw or 0)) for card_raw, units_raw in rows
    ]


def _pending_mb(conn: Connection, host: str, resource: str) -> int:
    """Units of ``resource`` still reserved on ``host`` — the pending
    holds a new claim must leave room for. Every held reservation counts
    in full until the reservation itself releases (``meta.reserved`` is
    popped at refund): the measured free is aggregate, so a drop below
    the claim-time level can't be attributed to the reserved job over an
    untracked consumer, and releasing on drops could free a hold whose
    job never allocated (td655171 review). Never cached: it must see the
    batch's own earlier claims and, once the caller holds the
    ``resource_slots`` row lock, every committed claim."""
    row = conn.execute(
        "SELECT COALESCE(SUM((meta->'reserved'->'slots'->>%s)::bigint), 0) "
        "FROM refs WHERE kind = 'job' "
        "AND meta->'reserved'->>'host' = %s "
        "AND meta->'reserved'->'slots' ? %s",
        (resource, host, resource),
    ).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def _card_holds(
    conn: Connection, host: str
) -> tuple[dict[int, int], dict[int, int], int, int]:
    """Per-card accounting of GPU holds on ``host``:
    ``(bound_pending_mb, bound_held, unbound_pending_mb, unbound_held)``.

    ``bound_pending_mb[i]`` is the ``vram_mb`` still reserved on card
    ``i`` — held units count in full until their reservation releases,
    never lessened by a measured drop (td655171 review).
    ``bound_held[i]`` is the count of still-held card-bound claims on
    ``i`` — the concurrency-cap count. Holds that consume a card but
    carry no ``gpu_card`` stamp — token-only GPU claims, or footprint
    claims admitted while the per-card probe was dark — aggregate into
    ``unbound_*``: the caller charges their units against the card
    under test, since an unbound job could be running on it.
    """
    pending: dict[int, int] = {}
    held: dict[int, int] = {}
    unbound_pending = 0
    unbound_held = 0
    for idx, units in _hold_rows(conn, host):
        if idx is None:
            unbound_pending += units
            unbound_held += 1
        else:
            pending[idx] = pending.get(idx, 0) + units
            held[idx] = held.get(idx, 0) + 1
    return pending, held, unbound_pending, unbound_held


def _pick_gpu_card(
    conn: Connection,
    host: str,
    cards_free: list[int],
    need_mb: int,
    reserve_mb: int,
) -> tuple[int | None, str | None]:
    """The card ``need_mb`` admits onto, and the veto reason when no
    card fits — the card-level half of live headroom (td655172).

    Card ``i``'s effective free is the beat's ``cards_free[i]`` less the
    ``vram_mb`` holds bound to ``i`` less *every* unbound hold's units —
    an unbound job could sit on any card, so the card under test carries
    the worst case; held units count in full until their reservations
    release (td655171 review). The list is raw measured free, so the pick
    also nets ``reserve_mb`` — the measured host's own
    ``PRECIS_HOST_VRAM_RESERVE_MB`` as stamped on ITS beat — off the
    card under test, the same subtraction the ``vram_mb`` capacity row
    and the ``vram_free_mb`` scalar carry (td655172 review): a raw-free
    check would let a claim spend the reserve when an
    untracked consumer is already eating into it, and the claimer's own
    env is the wrong source under a remote ``target_node``. Applying the
    host-wide
    reserve to each card under test mirrors the scalar's conservative
    application to the best card — it can only under-state headroom.
    ``i`` is eligible when its effective free
    covers ``need_mb`` AND its held-job count (bound plus the same
    unbound worst case) stays under :func:`gpu_card_max_jobs`. The
    freest eligible card wins (greedy first-fit). ``(None, "vram_mb")``
    when no card has headroom; ``(None, "vram_card_busy")`` when
    headroom survives only on cards at the concurrency cap.
    """
    pending, held, unbound_pending, unbound_held = _card_holds(conn, host)
    cap = gpu_card_max_jobs()
    reserve = reserve_mb
    pick: int | None = None
    pick_eff = -1
    headroom_somewhere = False
    for i, free in enumerate(cards_free):
        eff = free - pending.get(i, 0) - unbound_pending - reserve
        if eff < need_mb:
            continue
        headroom_somewhere = True
        if held.get(i, 0) + unbound_held >= cap:
            continue
        if eff > pick_eff:
            pick, pick_eff = i, eff
    if pick is None:
        return None, "vram_card_busy" if headroom_somewhere else "vram_mb"
    return pick, None


def admission_block(
    conn: Connection,
    *,
    host: str,
    units: dict[str, int],
    cache: MutableMapping[tuple[Any, ...], Any],
) -> tuple[str | None, int | None]:
    """``(veto_reason, gpu_card)`` — why ``units`` cannot admit on
    ``host`` right now, and the card index the claim binds when VRAM
    admits (``None`` when the beat carries no per-card list, leaving
    the dispatch unbound). The live-state veto on top of the ledger
    (gr595542 review, td655172). Checked
    right after the reservation decrement (the caller refunds on a
    veto); a blocked claim is skipped this round (the row's lock frees
    at commit and it retries next pass), mirroring the other claim
    vetoes:

    * ``"vram_card"`` — footprint + margin exceeds the host's
      ``vram_card_mb`` (largest single card): no card can hold the model,
      so no retry on this host helps until the model shrinks or the
      hardware changes.
    * ``"vram_mb"`` — no card's effective free can cover the units (or,
      with no per-card list, the freest single card's measured free
      can't): ``_pick_gpu_card`` / the scalar check, both less the
      *pending* holds the beat can't yet show. Effective free nets the
      measured host's ``PRECIS_HOST_VRAM_RESERVE_MB`` on both paths —
      the scalar probe subtracts it at measurement, the pick subtracts
      the beat's ``vram_reserve_mb`` stamp (the measured host's own
      setting — the claimer's env would be the wrong host's under a
      remote ``target_node``) from the raw per-card list's card under
      test (td655172 review).
    * ``"vram_card_busy"`` — a card could fit the units but every
      eligible card is at the :func:`gpu_card_max_jobs` concurrency cap.
    * ``"mem_mb"`` — the host's measured *admittable* free memory
      (available less the OS reserve, the same subtraction the static
      ``mem_mb`` row carries — td655171 review), less pending
      ``mem_mb`` holds, can't cover the units.

    Pending means *fully* held: a reservation's units count against live
    free for the reservation's whole life — the measured free is
    aggregate, so no drop can be attributed to the reserved job over an
    untracked consumer, and the hold releases only when the reservation
    itself refunds (td655171 review; the drop-evidenced release this
    replaces could free a hold whose job had not allocated). The
    residual is under-admission — a materialized job's units
    double-count once the beat shows its consumption — never
    over-admission.

    Call it after the reservation decrement, which serializes concurrent
    claimers on the ledger row, so the pending sum is current. The ledger
    rows are static totals minus reservations; untracked consumers don't
    decrement them, and this closes that gap. A missing measurement (no
    heartbeat meta key, no per-card row) reads as dark — the check stays
    out of the way until the data exists.
    """
    need_vram = units.get(VRAM_MB)
    if need_vram is not None:
        ceiling = _host_hard_capacity(conn, host, VRAM_CARD_MB, cache)
        if ceiling is not None and need_vram > ceiling:
            return "vram_card", None
    live, cards_free, _beat, reserve = _live_free_mb(conn, host, cache)
    picked: int | None = None
    if need_vram is not None and cards_free and reserve is not None:
        # The pick needs the measured host's reserve; a beat carrying
        # the list without the stamp (predating it) can't say what that
        # is — falling back to the claimer's env would subtract the
        # wrong host's (td655172 review), so the unbound scalar check
        # below stands instead of guessing.
        picked, veto = _pick_gpu_card(conn, host, cards_free, need_vram, reserve)
        if veto is not None:
            return veto, None
    for resource in (VRAM_MB, MEM_MB):
        need = units.get(resource)
        if need is None or (resource == VRAM_MB and picked is not None):
            continue  # VRAM admitted per-card above — the scalar check is subsumed
        free = live.get(resource)
        if free is None:
            continue
        if need > free - _pending_mb(conn, host, resource):
            return resource, None
    return None, picked


def admission_units(
    conn: Connection,
    *,
    executor: str,
    meta: dict[str, Any],
    res_host: str,
    requires: dict[str, int],
    advertised_here: Collection[str],
    cache: MutableMapping[tuple[Any, ...], Any],
) -> dict[str, int]:
    """Extra ``resource_slots`` units this claim must reserve, beyond
    its counted tokens — ``{}`` when admission is dark for the job.

    GPU (any executor): the job's :func:`effective_model` with a
    ``model_footprint`` row and both ``vram_mb`` and the per-card
    ``vram_card_mb`` ceiling advertised on ``res_host`` →
    ``{"vram_mb": footprint + margin}``. CPU (the science executor
    only): a ``(job_type, meta.size)`` rollup key with at least
    :func:`min_samples` measured runs and a ``mem_mb`` row advertised →
    ``{"mem_mb": ceil(p95 × margin)}``. The conditional decrement of
    the merged reservation then refuses the claim when the headroom
    isn't there, all-or-nothing with the tokens; :func:`admission_block`
    additionally vetoes against live state (per-card fit, measured free
    less pending admissions) right after the decrement, refunding it.

    ``cache`` dedupes the lookups across the candidates of one claim
    pass (keyed reads on tiny tables, but a deep queue shouldn't re-read
    per row); it lives for one :func:`claim_executor_jobs` call.
    """
    units: dict[str, int] = {}
    key: tuple[Any, ...]
    model = effective_model(meta)
    if "gpu" in requires and model:
        key = ("vram", str(model))
        if key not in cache:
            cache[key] = model_footprint_vram_mb(conn, str(model))
        footprint = cache[key]
        if (
            footprint is not None
            and VRAM_MB in advertised_here
            and VRAM_CARD_MB in advertised_here
        ):
            units[VRAM_MB] = _clamp_to_capacity(
                conn,
                res_host,
                VRAM_MB,
                int(footprint) + vram_margin_mb(),
                cache,
            )
    if executor == SCIENCE_EXECUTOR and MEM_MB in advertised_here:
        size = meta.get("size")
        size_name = str(size.get("name")) if isinstance(size, dict) else None
        size_value = str(size.get("value")) if isinstance(size, dict) else None
        key = ("rss", str(meta.get("job_type") or ""), size_name, size_value)
        if key not in cache:
            cache[key] = predicted_rss_mb(
                conn,
                key[1],
                size_name,
                size_value,
                min_samples=min_samples(),
            )
        rss_p95 = cache[key]
        if rss_p95 is not None:
            units[MEM_MB] = _clamp_to_capacity(
                conn,
                res_host,
                MEM_MB,
                math.ceil(rss_p95 * rss_margin_factor()),
                cache,
            )
    return units


def bound_device(meta: dict[str, Any]) -> str | None:
    """The torch device a card-bound claim pins its run to — ``cuda:<i>``
    when the pick stamped ``meta.reserved.gpu_card``, else ``None``
    (unbound: the backend's own device default). The in-process MLIP
    dispatches (``struct_relax``/``struct_neb``/``struct_md``/
    ``struct_search``) hand it to their calculator factories — the
    binding an env pin couldn't give an initialized CUDA context or a
    cached calculator (td655172 review, take two)."""
    raw = (meta.get("reserved") or {}).get("gpu_card")
    if raw is None:
        return None
    try:
        return f"cuda:{int(raw)}"
    except (TypeError, ValueError):
        return None


def note_model_use(conn: Connection, *, meta: dict[str, Any], res_host: str) -> None:
    """Stamp the reservation host's last-used model after a successful
    claim — the write half of the model-affinity sort key.

    No-op unless the job has an :func:`effective_model`: only
    model-carrying jobs (the science lane's GPU/MLIP computes) steer the
    queue, and an implicit default (``struct_relax`` → ``mace_mp``)
    stamps exactly what the run will load. Rides the claim transaction,
    so a rolled-back claim leaves no stamp.
    """
    model = effective_model(meta)
    if not model:
        return
    conn.execute(_UPSERT_APP_STATE, (f"{_LAST_MODEL_KEY_PREFIX}{res_host}", model))
    conn.execute(
        _UPSERT_APP_STATE,
        (f"{_LAST_MODEL_AT_KEY_PREFIX}{res_host}", str(time.time())),
    )


def last_models(
    conn: Connection, hosts: Collection[str], *, now: float | None = None
) -> dict[str, str]:
    """``host -> last-used model`` for every host whose stamp is fresh
    (younger than :func:`model_affinity_ttl_s`) — the read half of the
    affinity sort key. Stale or absent stamps are simply absent, so a
    host with cold weights steers nothing.

    ``now`` is injectable for tests; the default is wall-clock epoch
    seconds (the stamp's unit).
    """
    if not hosts:
        return {}
    now = time.time() if now is None else now
    rows = conn.execute(
        "SELECT key, value FROM app_state WHERE key = ANY(%s)",
        ([f"{_LAST_MODEL_KEY_PREFIX}{h}" for h in hosts],),
    ).fetchall()
    at_rows = conn.execute(
        "SELECT key, value FROM app_state WHERE key = ANY(%s)",
        ([f"{_LAST_MODEL_AT_KEY_PREFIX}{h}" for h in hosts],),
    ).fetchall()
    at_by_host = {str(k)[len(_LAST_MODEL_AT_KEY_PREFIX) :]: v for k, v in at_rows}
    ttl = model_affinity_ttl_s()
    out: dict[str, str] = {}
    for key, value in rows:
        host = str(key)[len(_LAST_MODEL_KEY_PREFIX) :]
        raw_at = at_by_host.get(host)
        try:
            at = float(str(raw_at))
        except (TypeError, ValueError):
            continue  # stamp without a parseable timestamp reads as cold
        if now - at <= ttl:
            out[host] = str(value)
    return out


__all__ = [
    "DEFAULT_GPU_CARD_MAX_JOBS",
    "DEFAULT_MIN_SAMPLES",
    "DEFAULT_MODEL_AFFINITY_TTL_MIN",
    "DEFAULT_RSS_MARGIN_FACTOR",
    "DEFAULT_VRAM_MARGIN_MB",
    "GPU_CARD_MAX_JOBS_ENV",
    "MIN_SAMPLES_ENV",
    "MODEL_AFFINITY_TTL_ENV",
    "RSS_MARGIN_ENV",
    "SCIENCE_EXECUTOR",
    "VRAM_MARGIN_ENV",
    "admission_block",
    "admission_units",
    "bound_device",
    "effective_model",
    "gpu_card_max_jobs",
    "last_models",
    "min_samples",
    "model_affinity_ttl_s",
    "note_model_use",
    "rss_margin_factor",
    "vram_margin_mb",
]

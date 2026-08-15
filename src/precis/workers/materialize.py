"""The demand materializer — §F cycle a
(``docs/backlog/cluster-scheduling.md`` §F, ~L280-306).

A scheduler :class:`~precis.workers.scheduler.Cadence` (300s, fleet-
singleton via the lease claim, mirroring ``health_digest``'s shape) that
mints a bounded batch of ``embed_batch`` jobs when the unembedded-chunk
backlog crosses a high-water mark, and does nothing otherwise. Hysteresis
is the pair of "mint only above HIGH" + "mint nothing while the previous
batch hasn't fully drained" — churn coalesces into few large batches by
construction rather than a trickle of tiny ones.

**ON by default (§F cycle b cutover).** :func:`run_materialize_pass` is
active unless ``PRECIS_MATERIALIZE_EMBED=0`` (or any non-truthy token) —
the standing ``embed`` pass has lost its rotation slot in
``registry.py`` (manual-only via ``--only embed``), so this materializer
→ ``embed_batch`` → ``job_inproc`` path is now the only thing draining
the embed queue in prod. Rollback: set ``PRECIS_MATERIALIZE_EMBED=0``
fleet-wide and run ``precis worker --only embed`` on any node (or revert
the ship) — the chunk queue is derived, so an outage delays
embeddings, never loses them.

**One small table, not a rewrite, for a second backlog source.**
:data:`_BACKLOG_SOURCES` is deliberately generic — `(name, job_type,
executor, count_fn, batch_limit, params_fn, …)` — so a new demand is a new
tuple entry, not new mint/hysteresis logic. Two policies now share it:

* **backlog high-water** (embed) — mint a bounded batch when the backlog
  count crosses HIGH and nothing is live; wait for it to drain.
* **job-queue band** (``is_band=True``; the SMALL-LLM ``derived_drain``
  sources, ``docs/backlog/small-llm-derived-drain-band.md``) — keep
  ``[low, high]`` live jobs per source while backlog remains. Default-OFF
  per-source. Two SMALL sources share one ``job_type`` (``derived_drain``),
  discriminated by ``params_pass`` (``meta.params.pass``) in every count.
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from precis.workers.runner import BatchResult

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

_HANDLER_NAME = "materialize"

_MATERIALIZE_EMBED_ENV = "PRECIS_MATERIALIZE_EMBED"
_BACKLOG_HIGH_ENV = "PRECIS_EMBED_BACKLOG_HIGH"
_MAX_JOBS_ENV = "PRECIS_EMBED_BATCH_MAX_JOBS"

_DEFAULT_BACKLOG_HIGH = 500
_DEFAULT_MAX_JOBS = 4
_DEFAULT_BATCH_LIMIT = 2000  # mirrors embed_batch's own params.limit default

#: A FAILED embed_batch job within this many minutes suppresses re-minting
#: — the mint-fail-loop guard (§F cycle a acceptance).
_FAILED_COOLDOWN_MINUTES = 15

_MINT_PRIO = 8  # background under 0014 ASC (lower = more urgent)

#: Promoted prio for a STUCK band's queued rows (Finding 5,
#: docs/backlog/llm-tier-ladder-cloud-cutover.md): below the background mint
#: prio (8), still above real work (prio <= 5) — see
#: :func:`_rebalance_stuck_band`.
_STARVED_PRIO = _MINT_PRIO - 2

#: Backlog-WARNING multiplier over PRECIS_EMBED_BACKLOG_HIGH — a
#: poor-man's liveness signal (until §D's real one): a queue piling up to
#: 4x the mint threshold despite (presumably) already-minted jobs means
#: something downstream of minting is stuck (embed_batch jobs not
#: claiming, or the embedder unreachable), not just normal churn.
_BACKLOG_WARN_MULTIPLIER = 4

#: The mint tick's wall-clock bucket width — mirrors ``scheduler.CADENCES``'s
#: "materialize" ``interval_s`` (300s, see ``workers/scheduler.py``), so a
#: concurrent/repeated mint landing in the same cadence window derives the
#: SAME ``idem_key`` set (see :func:`_mint_jobs`).
_TICK_BUCKET_S = 300

# ── SMALL-LLM derived-drain bands (docs/backlog/small-llm-derived-drain-band.md)
# A different minting POLICY from embed's backlog high-water: keep a BAND of
# 20–50 live `derived_drain` jobs per SMALL queue (summarize / classify) while
# backlog remains, so melchior's job_inproc always has a next low-prio job to
# claim. Per-source (each SMALL queue is its own band), default-OFF.
_SMALL_BAND_SUMMARIZE_ENV = "PRECIS_SMALL_BAND_SUMMARIZE"
_SMALL_BAND_CLASSIFY_ENV = "PRECIS_SMALL_BAND_CLASSIFY"
_SMALL_BAND_LOW_ENV = "PRECIS_SMALL_BAND_LOW"
_SMALL_BAND_HIGH_ENV = "PRECIS_SMALL_BAND_HIGH"
_SMALL_DRAIN_LIMIT_ENV = "PRECIS_SMALL_DRAIN_LIMIT"
_SMALL_DRAIN_CONCURRENCY_ENV = "PRECIS_SMALL_DRAIN_CONCURRENCY"
#: The host whose local SMALL model these jobs pin to (params.target_node — a
#: job_inproc claim-gate node pin). MUST match that host's PRECIS_NODE claim
#: identity, else the job strands unclaimed. Default melchior (the SMALL server).
_SMALL_TARGET_NODE_ENV = "PRECIS_SMALL_DRAIN_TARGET_NODE"

_DEFAULT_BAND_LOW = 20
_DEFAULT_BAND_HIGH = 50
_DEFAULT_SMALL_DRAIN_LIMIT = 500  # params.limit per derived_drain job
#: A thread-pool width for I/O fan-out against the (now cloud-only)
#: llm.chain.small endpoint — NOT a router local-serving slot cap (there is
#: no local slot to cap post the 2026-08-15 cloud cutover; see
#: workers/job_types/derived_drain.py's module docstring).
_DEFAULT_SMALL_DRAIN_CONCURRENCY = 16
_DEFAULT_SMALL_TARGET_NODE = "melchior"


def _env_int(name: str, default: int, *, floor: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(floor, int(raw))
    except ValueError:
        log.warning(
            "materialize: %s=%r is not an int; using default %d", name, raw, default
        )
        return default


def _small_band() -> tuple[int, int]:
    low = _env_int(_SMALL_BAND_LOW_ENV, _DEFAULT_BAND_LOW, floor=0)
    high = _env_int(_SMALL_BAND_HIGH_ENV, _DEFAULT_BAND_HIGH, floor=1)
    return (min(low, high), high)


def _small_drain_limit() -> int:
    """The per-job ``params.limit`` (chunks one ``derived_drain`` drains). ONE
    source of truth: both the minted params AND :func:`_materialize_band`'s
    "how many jobs does the backlog need" math read this, so raising
    ``PRECIS_SMALL_DRAIN_LIMIT`` can't desync them (a fixed ``src.batch_limit``
    would over-mint ~limit/500× no-op jobs)."""
    return _env_int(_SMALL_DRAIN_LIMIT_ENV, _DEFAULT_SMALL_DRAIN_LIMIT)


def _small_drain_params(pass_name: str) -> Callable[[int], dict[str, Any]]:
    """A ``params_fn`` for a SMALL derived_drain source — carries the pass
    discriminator, the per-job chunk limit, the in-pass concurrency (== the
    router slot cap), and the melchior host pin."""

    def _fn(_batch_limit: int) -> dict[str, Any]:
        params: dict[str, Any] = {
            "pass": pass_name,
            "limit": _small_drain_limit(),
            "concurrency": _env_int(
                _SMALL_DRAIN_CONCURRENCY_ENV, _DEFAULT_SMALL_DRAIN_CONCURRENCY
            ),
        }
        target = os.environ.get(_SMALL_TARGET_NODE_ENV, _DEFAULT_SMALL_TARGET_NODE)
        if target:
            params["target_node"] = target
        return params

    return _fn


def _summarize_backlog_count(store: Store) -> int:
    from precis.workers.llm_summarize import unsummarized_chunk_count

    with store.pool.connection() as conn:
        return unsummarized_chunk_count(conn)


def _classify_backlog_count(store: Store) -> int:
    from precis.workers.classify import unclassified_chunk_count

    with store.pool.connection() as conn:
        return unclassified_chunk_count(conn)


def materialize_embed_enabled() -> bool:
    """``PRECIS_MATERIALIZE_EMBED`` — default-ON as of §F cycle b (the
    cutover): unset ⇒ active, matching the standing ``embed`` pass losing
    its rotation slot (``registry.py``). ``PRECIS_MATERIALIZE_EMBED=0``
    (or any non-truthy token) is the documented opt-out/rollback — see
    the module docstring's rollback story."""
    from precis.utils.env import env_flag

    return env_flag(_MATERIALIZE_EMBED_ENV, default=True)


def _backlog_high() -> int:
    raw = os.environ.get(_BACKLOG_HIGH_ENV)
    if raw is None:
        return _DEFAULT_BACKLOG_HIGH
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning(
            "materialize: %s=%r is not an int; using default %d",
            _BACKLOG_HIGH_ENV,
            raw,
            _DEFAULT_BACKLOG_HIGH,
        )
        return _DEFAULT_BACKLOG_HIGH


def _max_jobs() -> int:
    raw = os.environ.get(_MAX_JOBS_ENV)
    if raw is None:
        return _DEFAULT_MAX_JOBS
    try:
        return max(1, int(raw))
    except ValueError:
        log.warning(
            "materialize: %s=%r is not an int; using default %d",
            _MAX_JOBS_ENV,
            raw,
            _DEFAULT_MAX_JOBS,
        )
        return _DEFAULT_MAX_JOBS


def _embed_backlog_count(store: Store) -> int:
    from precis.workers.embed import unembedded_chunk_count

    with store.pool.connection() as conn:
        return unembedded_chunk_count(conn)


@dataclass(frozen=True, slots=True)
class _BacklogSource:
    """One backlog source: how to count it, and how to mint against it.
    See the module docstring's "one small table" note.

    ``enabled_fn`` / ``band`` / ``params_pass`` (all default to the embed
    behaviour) add the SMALL-LLM derived-drain sources without touching embed:

    * ``enabled_fn`` — a per-source gate (embed stays gated by the pass-level
      ``materialize_embed_enabled``; SMALL sources gate on their own flags).
    * ``is_band`` — when True, this source uses the job-queue BAND policy
      (:func:`_materialize_band`, live ``[low, high]`` from :func:`_small_band`)
      instead of the backlog high-water policy: keep the band of live jobs full
      while backlog remains.
    * ``params_pass`` — the ``meta.params.pass`` discriminator, so two sources
      sharing one ``job_type`` (``derived_drain``) count their OWN live/failed
      jobs, not each other's.
    """

    name: str
    job_type: str
    executor: str
    count_fn: Callable[[Any], int]
    batch_limit: int
    params_fn: Callable[[int], dict[str, Any]]
    enabled_fn: Callable[[], bool] = lambda: True
    is_band: bool = False
    params_pass: str | None = None


_BACKLOG_SOURCES: tuple[_BacklogSource, ...] = (
    _BacklogSource(
        name="embed",
        job_type="embed_batch",
        executor="job_inproc",
        count_fn=_embed_backlog_count,
        batch_limit=_DEFAULT_BATCH_LIMIT,
        params_fn=lambda limit: {"limit": limit},
    ),
    # SMALL-LLM derived-drain bands — one `derived_drain` job_type, two queues
    # discriminated by params.pass. Default-OFF (enable per-source in Slice 4).
    _BacklogSource(
        name="summarize_drain",
        job_type="derived_drain",
        executor="job_inproc",
        count_fn=_summarize_backlog_count,
        batch_limit=_DEFAULT_SMALL_DRAIN_LIMIT,
        params_fn=_small_drain_params("llm_summarize"),
        enabled_fn=lambda: _band_source_enabled(_SMALL_BAND_SUMMARIZE_ENV),
        is_band=True,
        params_pass="llm_summarize",
    ),
    _BacklogSource(
        name="classify_drain",
        job_type="derived_drain",
        executor="job_inproc",
        count_fn=_classify_backlog_count,
        batch_limit=_DEFAULT_SMALL_DRAIN_LIMIT,
        params_fn=_small_drain_params("classify"),
        enabled_fn=lambda: _band_source_enabled(_SMALL_BAND_CLASSIFY_ENV),
        is_band=True,
        params_pass="classify",
    ),
)


def _band_source_enabled(env_name: str) -> bool:
    """A SMALL band source is live only when its flag is explicitly truthy —
    default-OFF (dark ship). Rides the ``materialize`` cadence, so the cadence
    itself must also be enabled (``PRECIS_MATERIALIZE_EMBED`` != 0)."""
    from precis.utils.env import env_flag

    return env_flag(env_name, default=False)


def _live_jobs(
    store: Store,
    job_type: str,
    *,
    params_pass: str | None = None,
    statuses: tuple[str, ...] = ("queued", "running"),
) -> int:
    """Count of ``job_type`` jobs currently in ``statuses`` (default
    ``queued``/``running``, any host) — "a bounded batch and no more until it
    drains". ``params_pass`` narrows to one ``meta.params.pass`` so two SMALL
    sources sharing the ``derived_drain`` job_type count their OWN live jobs,
    not each other's; ``statuses`` narrows further (e.g. ``("running",)`` for
    the "is anything actually draining?" stuck check)."""
    pass_clause = ""
    # Statement-order params: job_type, [optional pass], status-array.
    args: list[Any] = [job_type]
    if params_pass is not None:
        pass_clause = "AND r.meta->'params'->>'pass' = %s"
        args.append(params_pass)
    args.append(list(statuses))
    with store.pool.connection() as conn:
        row = conn.execute(
            f"""
            SELECT count(*) FROM refs r
             WHERE r.kind = 'job' AND r.deleted_at IS NULL
               AND r.meta->>'job_type' = %s
               {pass_clause}
               AND EXISTS (
                     SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                      WHERE rt.ref_id = r.ref_id
                        AND t.namespace = 'STATUS'
                        AND t.value = ANY(%s)
                   )
            """,
            tuple(args),
        ).fetchone()
    return int(row[0]) if row else 0


def _in_failed_cooldown(
    store: Store, job_type: str, *, params_pass: str | None = None
) -> bool:
    """True when the MOST RECENT terminal ``job_type`` job failed within
    :data:`_FAILED_COOLDOWN_MINUTES` — no mint-fail loop. ``params_pass``
    narrows to one ``meta.params.pass`` (same reason as :func:`_live_jobs`) so a
    failed summarize drain doesn't put classify's band into cooldown too.

    Keyed on ``ref_tags.created_at`` for the CURRENT ``STATUS:`` tag row,
    not ``refs.updated_at`` — ``set_status``'s ``replace_prefix=True``
    deletes the prior ``STATUS:`` row and inserts a fresh one, so this is
    exactly "when did this job reach its current terminal status", while
    ``refs.updated_at`` isn't bumped by every tag write and would be the
    wrong clock.
    """
    pass_clause = ""
    args: list[Any] = [job_type]
    if params_pass is not None:
        pass_clause = "AND r.meta->'params'->>'pass' = %s"
        args.append(params_pass)
    args.append(["succeeded", "failed", "cancelled"])
    with store.pool.connection() as conn:
        row = conn.execute(
            f"""
            SELECT t.value, rt.created_at
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'job' AND r.deleted_at IS NULL
               AND r.meta->>'job_type' = %s
               {pass_clause}
               AND t.namespace = 'STATUS'
               AND t.value = ANY(%s)
             ORDER BY rt.created_at DESC
             LIMIT 1
            """,
            tuple(args),
        ).fetchone()
    if row is None:
        return False
    status, tagged_at = row
    if status != "failed" or tagged_at is None:
        return False
    return bool(
        tagged_at > datetime.now(UTC) - timedelta(minutes=_FAILED_COOLDOWN_MINUTES)
    )


def _mint_jobs(store: Store, src: _BacklogSource, n: int) -> int:
    """Mint up to ``n`` bounded ``src.job_type`` jobs, ``idem_key``d per
    (tick, index) so a crashed materializer re-run — OR a manual ``--only
    materialize`` invocation racing the standing 300s cadence — can't
    double-mint within one tick. ``tick`` is a DETERMINISTIC 300-second
    wall-clock bucket (``int(time.time()) // _TICK_BUCKET_S``), not a
    fresh nonce per call: two invocations landing in the same cadence
    window MUST derive the same idem_key set, so the existence check
    below dedupes the second one to zero mints. (An earlier version
    minted a fresh ``uuid4`` tick per call — every invocation was
    therefore unique by construction, so the existence check could never
    fire across processes; only a crash-and-retry of the SAME Python call
    ever deduped.) Parentless by design (system-minted background
    maintenance — no owning todo/build-subject exists); the executors'
    failure-bubble already no-ops for a parentless job (see
    ``claude_inproc``'s "orphan jobs (legacy, no parent_id) just no-op").

    Residual: two processes racing the check-then-insert for the SAME
    (tick, i) within the same 300s bucket can still both pass the
    existence check before either commits — a classic check-then-insert
    race the deterministic tick narrows but doesn't close. Bounded to at
    most one extra batch for that window (not a runaway loop) and
    accepted as-is; closing it fully needs a unique index or an advisory
    lock, out of scope here.
    """
    from precis.store.types import Tag

    tick = int(time.time()) // _TICK_BUCKET_S
    minted = 0
    for i in range(n):
        # Keyed on src.NAME, not src.job_type — two SMALL sources share the
        # `derived_drain` job_type, so a job_type key would collide their mints
        # within a tick. (embed's key changes format once, harmless: idem only
        # dedupes within the 300s window.)
        idem_key = f"materialize:{src.name}:{tick}:{i}"
        with store.pool.connection() as conn:
            existing = conn.execute(
                "SELECT 1 FROM refs WHERE kind = 'job' AND deleted_at IS NULL "
                "AND meta->>'idem_key' = %s LIMIT 1",
                (idem_key,),
            ).fetchone()
            if existing is not None:
                conn.commit()
                continue
            ref = store.insert_ref(
                kind="job",
                slug=None,
                title=f"{src.job_type} (materializer {i + 1}/{n})",
                meta={
                    "job_type": src.job_type,
                    "executor": src.executor,
                    "params": src.params_fn(src.batch_limit),
                    "idem_key": idem_key,
                },
                prio=_MINT_PRIO,
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
        minted += 1
    return minted


def _rebalance_stuck_band(store: Store, src: _BacklogSource, *, stuck: bool) -> None:
    """Dynamic prio nudge for Finding 5
    (``docs/backlog/llm-tier-ladder-cloud-cutover.md``): ``claim_executor_jobs``
    (``workers/executors/_common.py``) orders ``COALESCE(prio,5) ASC, ref_id
    ASC``, and both SMALL derived-drain bands mint at the SAME ``_MINT_PRIO``
    — so a source that keeps re-minting (summarize) always beats a source
    whose band is full-but-stuck (classify) on the ``ref_id`` tiebreak,
    starving it forever rather than just until its own band drains. This
    UPDATEs the QUEUED rows of exactly ONE band (this ``src``'s ``job_type`` +
    ``params_pass``) to :data:`_STARVED_PRIO` while stuck, promoting it ahead
    of the still-minting sibling; ``stuck=False`` reverts to :data:`_MINT_PRIO`.
    The ``prio IS DISTINCT FROM`` guard makes a repeat call a no-op UPDATE
    (matches zero rows) rather than a needless write every cadence tick.

    Self-correcting: this only ever promotes THIS band's own queued rows, and
    :func:`_materialize_band` calls ``stuck=False`` the moment the band is no
    longer full-but-idle (band draining, or not full) — so the promotion
    reverts on its own once the stuck condition clears, no separate cleanup
    pass needed. Scoped to one ``job_type`` + ``params_pass``: the sibling
    SMALL band and any other ``job_type`` (e.g. ``embed_batch``) are untouched
    by the ``WHERE`` clause below. Accepted side effect: while promoted, this
    band's rows also outrank ``embed_batch``'s prio-8 rows on the shared
    ``job_inproc`` claim lane — small and self-limiting (only as many rows as
    the band width, only for as long as the stuck condition holds).
    """
    target = _STARVED_PRIO if stuck else _MINT_PRIO
    pass_clause = ""
    args: list[Any] = [target, src.job_type]
    if src.params_pass is not None:
        pass_clause = "AND r.meta->'params'->>'pass' = %s"
        args.append(src.params_pass)
    args.append(target)
    with store.pool.connection() as conn:
        conn.execute(
            f"""
            UPDATE refs r
               SET prio = %s
             WHERE r.kind = 'job' AND r.deleted_at IS NULL
               AND r.meta->>'job_type' = %s
               {pass_clause}
               AND r.prio IS DISTINCT FROM %s
               AND EXISTS (
                     SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                      WHERE rt.ref_id = r.ref_id
                        AND t.namespace = 'STATUS'
                        AND t.value = 'queued'
                   )
            """,
            tuple(args),
        )
        conn.commit()


def _materialize_band(store: Store, src: _BacklogSource, count: int) -> int:
    """Job-queue BAND policy (SMALL derived-drain sources): keep ``[low, high]``
    live jobs for THIS source while its backlog remains. When live drops below
    ``low`` and backlog > 0, top the band back up toward ``high`` (capped by how
    many jobs the current backlog can actually feed). Independent per source —
    an empty queue mints nothing; a summarize flood can't crowd classify out
    (each has its own band + its own live/failed counts via ``params_pass``)."""
    if count <= 0:
        return 0
    low, high = _small_band()
    live = _live_jobs(store, src.job_type, params_pass=src.params_pass)
    if live >= low:
        # Band full → not minting. That's NORMAL while jobs drain, but STUCK if
        # the band is full of jobs NOTHING is claiming (dead melchior worker or
        # a target_node that no host's claim-gate matches) while backlog remains
        # — the band-policy analogue of the high-water WARN (there's no
        # count-vs-4×HIGH here; "full but 0 running" is the precise signal).
        running = _live_jobs(
            store, src.job_type, params_pass=src.params_pass, statuses=("running",)
        )
        if running == 0:
            log.warning(
                "materialize: %s band full (%d live) but 0 running and "
                "backlog=%d — nothing is draining; check target_node pin / the "
                "melchior worker (band=[%d,%d])",
                src.name,
                live,
                count,
                low,
                high,
            )
            # Finding 5 fairness fix: promote this band's queued rows ahead of
            # a continuously-re-minted sibling band on the shared job_inproc
            # claim tiebreak — see _rebalance_stuck_band's docstring.
            _rebalance_stuck_band(store, src, stuck=True)
        else:
            _rebalance_stuck_band(store, src, stuck=False)
        return 0  # hysteresis: full enough, don't re-mint
    _rebalance_stuck_band(store, src, stuck=False)
    if _in_failed_cooldown(store, src.job_type, params_pass=src.params_pass):
        return 0
    # Top up toward HIGH, but never mint more jobs than the backlog can feed
    # (each job drains up to _small_drain_limit() chunks — the SAME figure the
    # minted params.limit carries, so the two can't desync) — else an empty-ish
    # queue mints a pile of jobs that each find nothing and no-op.
    want = high - live
    by_backlog = math.ceil(count / _small_drain_limit())
    n = max(1, min(want, by_backlog))
    minted = _mint_jobs(store, src, n)
    if minted:
        log.info(
            "materialize: %s band [%d,%d] live=%d backlog=%d — minted %d %s job(s)",
            src.name,
            low,
            high,
            live,
            count,
            minted,
            src.job_type,
        )
    return minted


def _materialize_one(store: Store, src: _BacklogSource) -> int:
    if not src.enabled_fn():
        return 0
    count = src.count_fn(store)
    if src.is_band:
        return _materialize_band(store, src, count)
    high = _backlog_high()
    if count > high * _BACKLOG_WARN_MULTIPLIER:
        # One line per tick (this function runs once per Cadence tick per
        # source) — surfaces in worker_logs and the §K console last-error
        # strip without a dedicated alert. live_jobs is included so an
        # operator can tell "stuck" (0 live — nothing is draining it)
        # from "draining a big legacy backlog" (>0 live — jobs are
        # minted and running, just haven't caught up yet); a separate
        # query from the hysteresis check below (only paid when this
        # WARNING branch actually fires) — doesn't change the fire
        # condition, which stays purely count-vs-high*4.
        log.warning(
            "materialize: %s backlog %d and not draining — check "
            "embed_batch jobs / embedder (high=%d, warn>%d, live_jobs=%d)",
            src.name,
            count,
            high,
            high * _BACKLOG_WARN_MULTIPLIER,
            _live_jobs(store, src.job_type),
        )
    if count <= high:
        return 0
    if _live_jobs(store, src.job_type) > 0:
        # A bounded batch and no more until it drains.
        return 0
    if _in_failed_cooldown(store, src.job_type):
        return 0
    n = max(1, min(math.ceil(count / src.batch_limit), _max_jobs()))
    minted = _mint_jobs(store, src, n)
    if minted:
        log.info(
            "materialize: %s backlog=%d > high=%d — minted %d %s job(s)",
            src.name,
            count,
            high,
            minted,
            src.job_type,
        )
    return minted


def run_materialize_pass(store: Store) -> BatchResult:
    """One materializer tick. A pure no-op unless
    :func:`materialize_embed_enabled` — see the module docstring's
    DARK-ship discipline."""
    if not materialize_embed_enabled():
        return BatchResult(handler=_HANDLER_NAME, claimed=0, ok=0, failed=0)

    minted_total = 0
    failed = 0
    for src in _BACKLOG_SOURCES:
        try:
            minted_total += _materialize_one(store, src)
        except Exception:
            failed += 1
            log.exception("materialize: backlog source %r raised", src.name)
    return BatchResult(
        handler=_HANDLER_NAME, claimed=minted_total, ok=minted_total, failed=failed
    )


__all__ = ["materialize_embed_enabled", "run_materialize_pass"]

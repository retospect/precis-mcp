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
executor, count_fn, batch_limit, params_fn)` — so a future non-embedding
demand is a new tuple entry, not new mint/hysteresis logic. Out of scope
for §F cycle a (embeddings is the only source today).
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from precis.workers.runner import BatchResult

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


def _embed_backlog_count(store: Any) -> int:
    from precis.workers.embed import unembedded_chunk_count

    with store.pool.connection() as conn:
        return unembedded_chunk_count(conn)


@dataclass(frozen=True, slots=True)
class _BacklogSource:
    """One backlog source: how to count it, and how to mint against it.
    See the module docstring's "one small table" note."""

    name: str
    job_type: str
    executor: str
    count_fn: Callable[[Any], int]
    batch_limit: int
    params_fn: Callable[[int], dict[str, Any]]


_BACKLOG_SOURCES: tuple[_BacklogSource, ...] = (
    _BacklogSource(
        name="embed",
        job_type="embed_batch",
        executor="job_inproc",
        count_fn=_embed_backlog_count,
        batch_limit=_DEFAULT_BATCH_LIMIT,
        params_fn=lambda limit: {"limit": limit},
    ),
)


def _live_jobs(store: Any, job_type: str) -> int:
    """Count of ``job_type`` jobs currently ``queued``/``running`` (any
    host) — "a bounded batch and no more until it drains"."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT count(*) FROM refs r
             WHERE r.kind = 'job' AND r.deleted_at IS NULL
               AND r.meta->>'job_type' = %s
               AND EXISTS (
                     SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                      WHERE rt.ref_id = r.ref_id
                        AND t.namespace = 'STATUS'
                        AND t.value = ANY(%s)
                   )
            """,
            (job_type, ["queued", "running"]),
        ).fetchone()
    return int(row[0]) if row else 0


def _in_failed_cooldown(store: Any, job_type: str) -> bool:
    """True when the MOST RECENT terminal ``job_type`` job failed within
    :data:`_FAILED_COOLDOWN_MINUTES` — no mint-fail loop.

    Keyed on ``ref_tags.created_at`` for the CURRENT ``STATUS:`` tag row,
    not ``refs.updated_at`` — ``set_status``'s ``replace_prefix=True``
    deletes the prior ``STATUS:`` row and inserts a fresh one, so this is
    exactly "when did this job reach its current terminal status", while
    ``refs.updated_at`` isn't bumped by every tag write and would be the
    wrong clock.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT t.value, rt.created_at
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'job' AND r.deleted_at IS NULL
               AND r.meta->>'job_type' = %s
               AND t.namespace = 'STATUS'
               AND t.value = ANY(%s)
             ORDER BY rt.created_at DESC
             LIMIT 1
            """,
            (job_type, ["succeeded", "failed", "cancelled"]),
        ).fetchone()
    if row is None:
        return False
    status, tagged_at = row
    if status != "failed" or tagged_at is None:
        return False
    return bool(
        tagged_at > datetime.now(UTC) - timedelta(minutes=_FAILED_COOLDOWN_MINUTES)
    )


def _mint_jobs(store: Any, src: _BacklogSource, n: int) -> int:
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
        idem_key = f"materialize:{src.job_type}:{tick}:{i}"
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


def _materialize_one(store: Any, src: _BacklogSource) -> int:
    count = src.count_fn(store)
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


def run_materialize_pass(store: Any) -> BatchResult:
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

"""Re-queue ``survey_extract`` after a paper's body is replaced (gr632959).

A ``survey_extract`` pass reads the chunks that exist when it runs. When
a body re-ingest later swaps those chunks (a front-matter-only stub body
replaced by the full text), the finished job card still reads as a
completed pass while the measures describe — or, for a stub, miss — a
body that no longer exists. This module is the repair:

* :func:`requeue_after_body_replace` — called inside the replacing
  transaction (``paper_reextract``'s swap) or by the drain below. A
  still-live ``survey_extract`` for the paper gets
  ``meta.survey_revision_request`` stamped — a durable revision
  request, deliberately NOT a checkpoint clear: an in-flight slice
  persists its own ``coordinator_state`` after this transaction commits
  and would silently restore a cleared checkpoint (the gr632959 review
  race). The request survives whatever the slice persists (the
  executor's meta merge never deletes keys), and
  ``survey_extract._survey_dispatch`` honours it at the next claim:
  one UPDATE swaps it for a durable ``restart`` checkpoint, so the
  pass restarts at chunk 0 of the new body regardless of phase and a
  mid-slice crash can't strand the stale checkpoint with the request
  already gone.
  A paper whose extraction finished gets a fresh ``survey_extract``
  minted under the ``survey-extract-requeue:<ref_id>`` idem key,
  carrying the latest prior job's params (minus its run token); a
  never-extracted paper is left alone — nobody asked it to extract, so
  the swap changes nothing.
* :func:`drain_pending_requeues` / :func:`run_survey_requeue_pass` —
  the ingest-side replace path
  (``db_writer.register_aliases_and_maybe_upgrade``'s ``markup_refetch``
  REPLACE) cannot mint jobs itself (``precis.ingest`` may not import
  ``precis.workers`` — the layering contract), so it stamps
  ``meta.survey_requeue_pending`` on the paper in the same transaction
  instead. This ref pass drains the flag on a cadence
  (``PRECIS_SURVEY_REQUEUE_REFRESH_HOURS``, default 1): each flagged
  paper is re-evaluated under a row lock, the flag cleared, and
  :func:`requeue_after_body_replace` run — a mid-flight replace simply
  re-stamps the flag for the next pass. The same pass also sweeps
  revision intent whose job went terminal underneath it: an in-flight
  slice's ``Done`` raced the stamp, or the consume's durable
  ``restart`` checkpoint outlived a slice that never returned (a
  coordinator exception, or a crash the stuck-job sweeper fails). The
  follow-up pass is minted idem-keyed and the marker cleared — or,
  when a still-live sibling is not guaranteed to re-read the body
  itself (parked at ``await_accept`` over the outgoing body, a
  markless legacy cursor, or a ``running`` slice already past its
  ``body_mark`` check with the outgoing chunks loaded — it can still
  finish into ``await_accept`` or ``Done`` without re-checking), the
  request transfers to it first — coverage judged on meta and STATUS
  read under the sibling's ``refs`` row lock, so a mid-inspection
  ``_persist_dispatch_result`` can't tear the pair into a false
  covered verdict (the td663790 review race): the marker only clears
  once coverage is minted, transferred or already present.

Paper identity is resolved the way the job itself resolves it
(``job_types.survey_extract._paper_id``): ``params.paper`` is a ref_id
(integer or digit string, whitespace- and zero-pad tolerant) or a
``pa<id>`` handle, where the id may also name a ref merged into the
paper — a handle minted before the merge still resolves to the survivor
today (:meth:`Store.resolve_handle` follows ``meta.superseded_by``), so
the lookup walks that chain backwards.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from precis.store import Store
from precis.workers._mint import mint_job_once
from precis.workers._throttle import due as _throttle_due
from precis.workers.runner import BatchResult

__all__ = [
    "drain_pending_requeues",
    "requeue_after_body_replace",
    "run_survey_requeue_pass",
]

log = logging.getLogger(__name__)

_JOB_TYPE = "survey_extract"
#: Params a re-run must not inherit: the old run's batch token.
_DROP_PARAMS = ("run_token",)
#: Terminal job statuses — mirrors ``executors._common.TERMINAL``; kept
#: as a literal so this module never imports the executor layer.
_TERMINAL = ("succeeded", "failed", "cancelled", "timed-out")
#: Live STATUS values under which no dispatch slice is executing —
#: mirrors ``executors._common.LEAVE_RUNNING`` (``queued``, ``yielded``,
#: ``waiting_*``), kept as literals like ``_TERMINAL``. Anything else —
#: ``running``, ``cancel_requested``, or a value this module does not
#: know — may be a slice in flight past its last ``body_mark``
#: re-check, so coverage that depends on a future check must not be
#: assumed for it.
_NO_INFLIGHT_SLICE = frozenset(
    {
        "queued",
        "yielded",
        "waiting_children",
        "waiting_time",
        "waiting_ask_user",
        "waiting_manual_kick",
    }
)
#: ``refs.meta`` flag the ingest-side replace path stamps; drained here.
_PENDING_FLAG = "survey_requeue_pending"
#: ``refs.meta`` key stamped on a LIVE ``survey_extract`` by
#: :func:`requeue_after_body_replace` — the durable revision request the
#: next dispatch slice honours. Literal shared with
#: ``precis.workers.job_types.survey_extract`` (kept as a literal in
#: both directions so neither module imports the other).
_REVISION_KEY = "survey_revision_request"
_IDEM_PREFIX = "survey-extract-requeue"
#: ``app_state`` key + env knob for the drain cadence (the
#: ``PRECIS_<X>_REFRESH_HOURS`` throttle idiom, ``_throttle.due``).
_STATE_KEY = "survey_requeue:last_run"
_REFRESH_ENV_VAR = "PRECIS_SURVEY_REQUEUE_REFRESH_HOURS"
_DEFAULT_REFRESH_HOURS = 1.0

#: The ``survey_extract`` job rows whose ``params.paper`` resolves to
#: ``%s`` — the paper's ref_id or a ref merged into it
#: (``meta.superseded_by`` chains, walked backwards; ``UNION`` dedup
#: ends cycles). ``paper`` resolves exactly the way
#: ``survey_extract._paper_id`` resolves it: digits or a ``pa<digits>``
#: handle (whitespace/zero-pad tolerant), nothing else — a plain slug
#: never resolved there, so none is matched here.
_PAPER_JOBS_SQL = """
WITH RECURSIVE ids AS (
    SELECT %s::bigint AS ref_id
    UNION
    SELECT r.ref_id
      FROM refs r
      JOIN ids ON (r.meta ->> 'superseded_by') ~ '^(pa)?[0-9]+$'
              AND regexp_replace(r.meta ->> 'superseded_by', '^pa', '')::bigint
                  = ids.ref_id
)
SELECT j.ref_id, j.meta -> 'params' AS params,
       NOT EXISTS (
             SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
              WHERE rt.ref_id = j.ref_id
                AND t.namespace = 'STATUS'
                AND t.value = ANY(%s)
       ) AS live
  FROM refs j
 WHERE j.kind = 'job' AND j.retired_at IS NULL
   AND j.meta ->> 'job_type' = %s
   AND (
        (btrim(j.meta -> 'params' ->> 'paper') ~ '^[0-9]+$'
         AND btrim(j.meta -> 'params' ->> 'paper')::bigint
             IN (SELECT ref_id FROM ids))
     OR (btrim(j.meta -> 'params' ->> 'paper') ~ '^pa[0-9]+$'
         AND substr(btrim(j.meta -> 'params' ->> 'paper'), 3)::bigint
             IN (SELECT ref_id FROM ids))
       )
 ORDER BY j.ref_id DESC
"""


def _survey_jobs(conn: Any, ref_id: int) -> list[tuple[int, dict[str, Any], bool]]:
    """Every live ``survey_extract`` job whose ``params.paper`` resolves
    to ``ref_id`` — newest first, ``(job_ref_id, params, is_live)``."""
    rows = conn.execute(
        _PAPER_JOBS_SQL, (ref_id, list(_TERMINAL), _JOB_TYPE)
    ).fetchall()
    return [
        (int(r[0]), r[1] if isinstance(r[1], dict) else {}, bool(r[2])) for r in rows
    ]


def requeue_after_body_replace(
    store: Store, conn: Any, ref_id: int, *, new_chunks: int
) -> int | None:
    """Cover ``ref_id``'s freshly swapped body with a complete extract pass.

    Runs on the caller's ``conn`` so the decision commits with the body
    swap. Three cases:

    * a **live** ``survey_extract`` targets the paper (queued, parked or
      running — a requeue this module minted, or the original pass
      still in flight): ``meta.survey_revision_request`` is stamped so
      the next slice re-enters ``extract`` at chunk 0 of the new body.
      This is deliberately not a ``coordinator_state`` clear: an
      in-flight slice persists its own state after this transaction
      commits and would silently restore the checkpoint, and a slice
      that persists ``Done``/``await_accept`` for the outgoing body
      never re-reads ``body_mark`` at all — the durable request survives
      both (the drain sweep mints the follow-up when the job went
      terminal). Returns that job's ref_id; no second extractor is
      minted.
    * the paper's extraction already finished: mint a fresh
      ``survey_extract`` under the ``survey-extract-requeue:<ref_id>``
      idem key, reusing the latest prior job's params minus
      ``run_token`` (a re-run under the old token would fold the new
      body's rows into the old batch). Returns the job's ref_id.
    * the paper never had a ``survey_extract``: returns ``None`` — a
      paper nobody asked to extract stays unextracted.
    """
    if new_chunks <= 0:
        return None
    jobs = _survey_jobs(conn, ref_id)
    live = next((j for j in jobs if j[2]), None)
    if live is not None:
        job_id = live[0]
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
            (
                Jsonb(
                    {
                        _REVISION_KEY: {
                            "paper": ref_id,
                            "requested_at": datetime.now(UTC).isoformat(),
                            "chunks": new_chunks,
                        }
                    }
                ),
                job_id,
            ),
        )
        log.info(
            "survey_requeue: ref_id=%s body replaced (%d chunk(s)); live "
            "survey_extract job %s revision-requested — restarts over the "
            "new body at its next claim",
            ref_id,
            new_chunks,
            job_id,
        )
        return job_id
    if not jobs:
        return None
    job_id = _mint_followup(store, conn, ref_id, jobs[0][1])
    log.info(
        "survey_requeue: ref_id=%s body replaced (%d chunk(s)); survey_extract job %s",
        ref_id,
        new_chunks,
        job_id,
    )
    return job_id


def _mint_followup(
    store: Store, conn: Any, ref_id: int, prior_params: dict[str, Any]
) -> int:
    """Mint the idem-keyed follow-up pass for a paper whose extraction
    finished — before the swap (the finished branch above) or raced to
    ``Done`` past it (the drain sweep)."""
    params = {k: v for k, v in prior_params.items() if k not in _DROP_PARAMS}
    params["paper"] = ref_id
    job_id, _reused = mint_job_once(
        store,
        conn=conn,
        job_type=_JOB_TYPE,
        executor="coordinator",
        title=f"survey_extract pa{ref_id} (body re-ingested)",
        params=params,
        idem_key=f"{_IDEM_PREFIX}:{ref_id}",
    )
    return job_id


def drain_pending_requeues(store: Store, *, limit: int | None = None) -> int:
    """Mint/reset for every paper stamped ``meta.survey_requeue_pending``,
    then sweep revision requests whose job went terminal (the ``Done``
    race). Returns the number of flags drained plus requests swept.

    Each flag is consumed under a ``FOR UPDATE`` row lock (``SKIP
    LOCKED`` past a mid-flight replace — its re-stamp lands next pass),
    so the clear and the mint/reset commit together.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs "
            "WHERE kind = 'paper' AND retired_at IS NULL AND meta ? %s "
            "ORDER BY ref_id",
            (_PENDING_FLAG,),
        ).fetchall()
    ref_ids = [int(r[0]) for r in rows]
    if limit is not None:
        ref_ids = ref_ids[:limit]
    drained = 0
    for ref_id in ref_ids:
        with store.tx() as conn:
            row = conn.execute(
                "SELECT 1 FROM refs "
                "WHERE ref_id = %s AND retired_at IS NULL AND meta ? %s "
                "FOR UPDATE SKIP LOCKED",
                (ref_id, _PENDING_FLAG),
            ).fetchone()
            if row is None:
                continue  # consumed already, or locked by a mid-flight replace
            n_body_row = conn.execute(
                "SELECT count(*) FROM chunks "
                "WHERE ref_id = %s AND ord >= 0 AND retired_at IS NULL",
                (ref_id,),
            ).fetchone()
            n_body = int(n_body_row[0]) if n_body_row else 0
            requeue_after_body_replace(store, conn, ref_id, new_chunks=n_body)
            conn.execute(
                "UPDATE refs SET meta = meta - %s WHERE ref_id = %s",
                (_PENDING_FLAG, ref_id),
            )
            drained += 1
    if drained:
        log.info("survey_requeue: drained %d pending flag(s)", drained)
    return drained + _sweep_revision_requests(store, limit=limit)


#: Revision-intent sweep — every ``survey_extract`` job row carrying
#: either marker: an unconsumed ``meta.survey_revision_request`` (the
#: ``Done`` race) or a consumed-but-never-returned
#: ``coordinator_state.restart`` (a coordinator exception or a crash —
#: see :func:`_sweep_revision_requests`). STATUS is deliberately not
#: selected here: the loop re-reads it under each row's lock, which is
#: the only read that can't race that job's own transition.
_REVISION_JOBS_SQL = """
SELECT j.ref_id, j.meta -> %s AS req,
       j.meta -> 'coordinator_state' -> 'restart' AS restart
  FROM refs j
 WHERE j.kind = 'job' AND j.retired_at IS NULL
   AND j.meta ->> 'job_type' = %s
   AND (j.meta ? %s OR j.meta -> 'coordinator_state' ? 'restart')
 ORDER BY j.ref_id
"""


def _job_status(conn: Any, ref_id: int) -> str:
    """The row's ``STATUS`` tag value, ``'queued'`` when untagged."""
    row = conn.execute(
        "SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
        "WHERE rt.ref_id = %s AND t.namespace = 'STATUS' LIMIT 1",
        (ref_id,),
    ).fetchone()
    return str(row[0]) if row else "queued"


def _revision_covered(meta: Any, status: str) -> bool:
    """True when a LIVE ``survey_extract`` will (re)read the current body
    on its own — only then may a terminal sibling's intent be cleared.

    Called with ``meta`` and ``status`` both read under the sibling's
    ``refs`` row lock (``SELECT ... FOR UPDATE`` in the sweep): every
    transition that can park the job into a no-inflight STATUS —
    ``_persist_dispatch_result``'s Yield persist, ``yield_claim``, the
    wake_runner's re-queue — writes ``refs`` in the same transaction as
    the STATUS flip, so the locked pair can't tear a stale extract
    checkpoint against a fresh parked status (the gr632959 td663790
    review race).

    Covered regardless of status: durable intent — an unconsumed
    ``survey_revision_request`` (its next claim restarts over the new
    body) or a ``coordinator_state.restart`` checkpoint (the request
    was consumed; the in-flight slice IS the restarted pass). Any other
    coverage needs a guaranteed FUTURE mark check, which exists only
    while no slice is in flight (``status`` in ``_NO_INFLIGHT_SLICE``):
    a never-claimed job reads whatever body exists at its first claim,
    and a parked extract cursor carrying ``body_mark`` re-derives the
    mark at its next claim and restarts on drift (gr632959).

    NOT covered: ``running`` / ``cancel_requested`` / an unknown status
    without durable intent — the in-flight slice already passed the
    mark check with the outgoing chunks loaded and can still finish
    into ``await_accept`` or ``Done`` without re-checking (the
    gr632959 review's running-sibling race); ``await_accept`` — the
    pass finished reading the outgoing body and that phase never
    consults ``body_mark``; and a legacy extract cursor without a mark
    (a pre-gr632959 checkpoint resumes blind at the stale ``idx``)."""
    if not isinstance(meta, dict):
        return False
    if meta.get(_REVISION_KEY) is not None:
        return True
    state = meta.get("coordinator_state")
    if isinstance(state, dict) and state.get("restart") is not None:
        return True
    if status not in _NO_INFLIGHT_SLICE:
        return False
    if not isinstance(state, dict) or not state:
        return True
    return state.get("phase") == "extract" and state.get("body_mark") is not None


def _sweep_revision_requests(store: Store, *, limit: int | None = None) -> int:
    """Mint the follow-up pass for terminal jobs whose revision intent
    outlived their last slice (the gr632959 review's ``Done`` race, and
    its r2 never-returned race).

    A live ``survey_extract`` consumes its own request at the next claim
    (``survey_extract._survey_dispatch`` atomically swaps the key for a
    durable ``restart`` checkpoint and restarts the pass), so live rows
    are skipped here. The intent marker is either:

    * ``meta.survey_revision_request`` still on the row — an in-flight
      slice persisted ``Done`` for the outgoing body after the stamp,
      so the job went terminal with the request unclaimed; or
    * ``coordinator_state.restart`` — the request WAS consumed, but the
      slice never returned: the dispatch writes the marker before the
      phase runs, a ``Yield`` persist overwrites it away and a ``Done``
      return deletes it, so a terminal job still carrying it raised
      mid-dispatch (the executor fails the job) or crashed mid-slice
      (the stuck-job sweeper fails it at ``PRECIS_STUCK_JOB_HOURS`` —
      crashed ``running`` jobs are never resumed).

    For either, the sweep mints the fresh pass (idem-keyed, so a double
    sweep collapses into the same job) and clears both markers. When a
    sibling pass is still live for the paper the intent is NOT simply
    dropped — a live job covers the swap only when its own state will
    re-read the new body (:func:`_revision_covered`); a pass parked at
    ``await_accept`` over the outgoing body never re-enters ``extract``,
    and a ``running`` sibling may be a slice already past its
    ``body_mark`` check over the outgoing chunks — it can still finish
    into ``await_accept`` or ``Done`` without re-checking — so the
    request is stamped onto each uncovered sibling (transferred) before
    the terminal job's markers clear.

    The coverage pair — the sibling's ``meta`` and its live STATUS —
    is read under the sibling's ``refs`` row lock (``FOR UPDATE``, not
    ``SKIP LOCKED``: a locked sibling is a transition in flight and the
    verdict has to wait for it, not guess). Without the lock a
    ``_persist_dispatch_result`` committing an ``await_accept`` park
    between the two reads hands ``_revision_covered`` a torn pair —
    the pre-park extract checkpoint (``body_mark`` present) plus the
    fresh parked status — and a covered verdict clears the terminal
    job's intent while the sibling finishes the stale pass, losing the
    rerun (the td663790 review finding). Statuses outside
    ``_NO_INFLIGHT_SLICE`` fail coverage on their own, so the only
    transitions that matter are the ones into that set — and each of
    those writes ``refs`` in the same transaction as the STATUS flip
    (Yield persist's ``coordinator_state`` checkpoint, ``yield_claim``,
    the wake_runner's ``FOR UPDATE`` + ``_clear_lease``), so the row
    lock is exactly the serialization point the inspection needs.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            _REVISION_JOBS_SQL, (_REVISION_KEY, _JOB_TYPE, _REVISION_KEY)
        ).fetchall()
    pending = [(int(r[0]), r[1], r[2]) for r in rows]
    if limit is not None:
        pending = pending[:limit]
    swept = 0
    for job_id, req, restart in pending:
        intent = req if isinstance(req, dict) else restart
        paper = intent.get("paper") if isinstance(intent, dict) else None
        if not isinstance(paper, int) or isinstance(paper, bool):
            continue  # foreign shape — leave it visible for an operator
        with store.tx() as conn:
            row = conn.execute(
                "SELECT 1 FROM refs WHERE ref_id = %s AND retired_at IS NULL "
                "AND (meta ? %s OR meta -> 'coordinator_state' ? 'restart') "
                "FOR UPDATE SKIP LOCKED",
                (job_id, _REVISION_KEY),
            ).fetchone()
            if row is None:
                continue  # consumed by dispatch, or locked elsewhere
            if _job_status(conn, job_id) not in _TERMINAL:
                continue  # live — dispatch consumes it at the next claim
            jobs = _survey_jobs(conn, paper)
            live_ids = [jid for jid, _params, live in jobs if live]
            if jobs and not live_ids:
                fresh = _mint_followup(store, conn, paper, jobs[0][1])
                log.info(
                    "survey_requeue: job %s went terminal holding revision "
                    "intent; swept into follow-up job %s",
                    job_id,
                    fresh,
                )
            elif live_ids:
                # gr632959 review r3+r4: a live sibling covers the swap
                # only when its own state will re-read the new body.
                # Transfer the request to each that cannot (a pass parked
                # at await_accept over the outgoing body, a markless
                # legacy cursor, or a running slice past its last
                # body_mark check) — clearing here would drop the rerun
                # on the floor while the sibling finishes its stale pass.
                transferred = []
                for live_id in live_ids:
                    # td663790 review: the coverage pair is read under
                    # the sibling's row lock — without it a concurrent
                    # ``_persist_dispatch_result`` parking the sibling at
                    # ``await_accept`` commits between the meta read and
                    # the STATUS read, tearing a stale extract checkpoint
                    # against the fresh parked status into a false
                    # covered verdict. ``FOR UPDATE`` (blocking) is the
                    # point: a locked sibling is a transition in flight
                    # and the verdict must wait for it.
                    lrow = conn.execute(
                        "SELECT meta FROM refs WHERE ref_id = %s FOR UPDATE",
                        (live_id,),
                    ).fetchone()
                    status = _job_status(conn, live_id)
                    if _revision_covered(lrow[0] if lrow else None, status):
                        continue
                    conn.execute(
                        "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
                        (
                            Jsonb(
                                {
                                    _REVISION_KEY: {
                                        **intent,
                                        "transferred_from": job_id,
                                    }
                                }
                            ),
                            live_id,
                        ),
                    )
                    transferred.append(live_id)
                if transferred:
                    log.info(
                        "survey_requeue: job %s went terminal holding "
                        "revision intent; transferred the request to "
                        "uncovered live job(s) %s",
                        job_id,
                        transferred,
                    )
            conn.execute(
                "UPDATE refs SET meta = (meta - %s) "
                "#- '{coordinator_state,restart}'::text[] WHERE ref_id = %s",
                (_REVISION_KEY, job_id),
            )
            swept += 1
    if swept:
        log.info("survey_requeue: swept %d revision request(s)", swept)
    return swept


def _due(store: Store) -> bool:
    """True when the drain cadence has elapsed since the last pass."""
    return _throttle_due(store, _STATE_KEY, _REFRESH_ENV_VAR, _DEFAULT_REFRESH_HOURS)


def run_survey_requeue_pass(store: Store, *, limit: int | None = None) -> BatchResult:
    """The ``survey_requeue`` ref pass: drain pending requeue flags if due.

    ``claimed`` / ``ok`` count flags drained plus revision requests
    swept; ``failed`` stays 0 (a per-flag fault rolls its own
    transaction back, the flag stays, and the next pass retries it).
    """
    idle = BatchResult(handler="survey_requeue", claimed=0, ok=0, failed=0)
    if not store.dsn or not _due(store):
        return idle
    drained = drain_pending_requeues(store, limit=limit)
    store.set_setting(_STATE_KEY, datetime.now(UTC).isoformat())
    return BatchResult(handler="survey_requeue", claimed=drained, ok=drained, failed=0)

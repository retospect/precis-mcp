"""Worker orchestration: claim → process → write loop.

Two entry points:

* :func:`run_handler_once` — drain *one* batch for *one* handler in
  *one* transaction. The unit tests pin behaviour against this.
* :func:`run_loop` — round-robin across a list of handlers, polling
  each one until it returns zero claimed rows; sleep when all
  handlers are idle. Used by the ``precis worker`` CLI.

The runner deliberately catches :class:`Exception` around each
chunk's :meth:`process` call: a single bad chunk should never crash
the loop. The handler's :meth:`write_failed` records the failure
marker so the chunk is not re-claimed; the next chunk in the batch
proceeds normally.

Database errors during ``write_ok`` / ``write_failed`` *do*
propagate — they indicate something deeper (connection lost, schema
drift, disk full) where blind retry is more harm than good.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from precis.embedder import EmbedderUnavailable
from precis.store import Store
from precis.workers.base import WorkerHandler

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BatchResult:
    """Outcome of one :func:`run_handler_once` call."""

    handler: str
    claimed: int
    ok: int
    failed: int


def run_handler_once(
    handler: WorkerHandler, store: Store, *, batch_size: int = 32
) -> BatchResult:
    """Claim and process up to ``batch_size`` chunks for ``handler``.

    All work for the batch happens inside a single connection /
    transaction:

    1. ``claim_batch`` selects + locks chunks ``FOR UPDATE OF c
       SKIP LOCKED``.
    2. For each row, ``process`` runs (pure compute).
    3. On success → ``write_ok``; on exception → ``write_failed``.
    4. Commit releases the row locks.

    Returns a :class:`BatchResult` with row counts. ``claimed=0``
    is the "no work" signal :func:`run_loop` uses to schedule a
    sleep.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    n_ok = 0
    n_failed = 0

    with store.pool.connection() as conn:
        rows = handler.claim_batch(conn, limit=batch_size)
        if not rows:
            conn.commit()
            return BatchResult(handler=handler.name, claimed=0, ok=0, failed=0)

        # ``process_batch`` returns a list parallel to ``rows`` where
        # each element is either the payload or an Exception. The
        # default implementation calls ``process`` per row; handlers
        # that benefit from bulk compute (EmbedHandler runs one
        # batched transformer forward pass per call) override it.
        # Per-row failures still route to write_failed below so the
        # poison-pill chunk gets a failure marker.
        try:
            results = handler.process_batch(rows)
        except EmbedderUnavailable as exc:
            # The embedder is transiently down/busy. Roll the tx back
            # (release the row locks, write *no* failure markers) and
            # report a clean no-progress batch so the run-loop sleeps
            # and re-claims these rows next cycle. This is a deferral,
            # not a failure: a restart / 429 burst must not stamp every
            # claimed chunk ``failed`` (that lit the status panel with
            # noise and forced needless re-embeds). ``claimed=0`` keeps
            # it out of the "any work this cycle?" tally so the node
            # backs off instead of hot-looping the down embedder.
            conn.rollback()
            log.warning(
                "worker: %s deferred batch of %d — embedder unavailable (%s); "
                "will retry next cycle",
                handler.name,
                len(rows),
                exc,
            )
            return BatchResult(handler=handler.name, claimed=0, ok=0, failed=0)
        for row, payload in zip(rows, results, strict=True):
            if isinstance(payload, Exception):
                log.warning(
                    "worker: %s process failed for chunk %d: %s",
                    handler.name,
                    row.chunk_id,
                    payload,
                )
                handler.write_failed(conn, row.chunk_id, repr(payload))
                n_failed += 1
            else:
                handler.write_ok(conn, row.chunk_id, payload)
                n_ok += 1
        conn.commit()

    return BatchResult(
        handler=handler.name,
        claimed=len(rows),
        ok=n_ok,
        failed=n_failed,
    )


#: A ref-level pass. Same return shape as :class:`BatchResult` so
#: the run-loop's logging path is uniform across chunk-level
#: handlers and ref-level passes. Called with the current
#: ``batch_size`` so callers can self-throttle the same way
#: chunk handlers do.
RefPass = Callable[[int], BatchResult]


def run_loop(
    handlers: list[WorkerHandler],
    store: Store,
    *,
    batch_size: int = 32,
    idle_seconds: float = 2.0,
    once: bool = False,
    should_stop: Callable[[], bool] | None = None,
    ref_passes: list[RefPass] | None = None,
) -> None:
    """Drive ``handlers`` (chunk-level) and ``ref_passes`` (ref-level)
    in round-robin until stopped or drained.

    Handlers run first per cycle so the chunk-level derived queue
    drains before the ref-level passes consume their output (in the
    common case where ``segment_toc`` needs ``chunk_embeddings`` to
    have landed for a paper before it can build segments).

    Termination:

    * ``once=True`` — make one full pass across all handlers and
      ref-passes and return.
    * ``once=False`` — loop forever. When every handler AND every
      ref-pass reports ``claimed=0`` in a pass, sleep
      ``idle_seconds`` then re-poll.
    * ``should_stop()`` — called between handlers and between
      ref-passes; returning ``True`` breaks out cleanly. The CLI
      wires this to a SIGINT/SIGTERM flag so ``Ctrl-C`` returns
      within one batch.
    """
    if not handlers and not ref_passes:
        log.warning("worker: no handlers and no ref_passes registered; nothing to do")
        return

    while True:
        any_work = False
        for handler in handlers:
            if should_stop is not None and should_stop():
                log.info("worker: stop signal received; exiting loop")
                return
            try:
                result = run_handler_once(handler, store, batch_size=batch_size)
            except Exception:
                # DB-side failure (connection drop, etc.). Log and
                # carry on to the next handler — we don't want a
                # single hiccup to kill the whole worker.
                log.exception("worker: %s batch raised; continuing", handler.name)
                continue
            # Structured payload travels via the DBLogHandler's
            # ``extra={'payload': ...}`` channel so worker_logs.payload
            # carries the BatchResult shape directly — `precis logs
            # --pass X` filters can roll up claimed / ok / failed
            # without re-parsing the message.
            log.info(
                "worker: %s claimed=%d ok=%d failed=%d",
                result.handler,
                result.claimed,
                result.ok,
                result.failed,
                extra={
                    "payload": {
                        "handler": result.handler,
                        "claimed": result.claimed,
                        "ok": result.ok,
                        "failed": result.failed,
                    }
                },
            )
            if result.claimed > 0:
                any_work = True

        for ref_pass in ref_passes or ():
            if should_stop is not None and should_stop():
                log.info("worker: stop signal received; exiting loop")
                return
            try:
                result = ref_pass(batch_size)
            except Exception:
                log.exception("worker: ref-pass raised; continuing")
                continue
            log.info(
                "worker: %s claimed=%d ok=%d failed=%d",
                result.handler,
                result.claimed,
                result.ok,
                result.failed,
                extra={
                    "payload": {
                        "handler": result.handler,
                        "claimed": result.claimed,
                        "ok": result.ok,
                        "failed": result.failed,
                    }
                },
            )
            if result.claimed > 0:
                any_work = True

        if once:
            return
        if not any_work:
            # Nothing to do anywhere; sleep before re-poll. Honour
            # ``should_stop`` while sleeping so ``Ctrl-C`` doesn't
            # have to wait the full interval.
            slept = 0.0
            tick = min(0.25, idle_seconds)
            while slept < idle_seconds:
                if should_stop is not None and should_stop():
                    return
                time.sleep(tick)
                slept += tick


__all__ = ["BatchResult", "RefPass", "run_handler_once", "run_loop"]

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

        for row in rows:
            try:
                payload = handler.process(row)
            except Exception as exc:
                # Pure-compute error: poison-pill chunk, oversize,
                # OOM under bge-m3, etc. Record the failure marker
                # so the loop doesn't re-claim the chunk forever.
                log.warning(
                    "worker: %s process failed for chunk %d: %s",
                    handler.name,
                    row.chunk_id,
                    exc,
                )
                handler.write_failed(conn, row.chunk_id, repr(exc))
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


def run_loop(
    handlers: list[WorkerHandler],
    store: Store,
    *,
    batch_size: int = 32,
    idle_seconds: float = 2.0,
    once: bool = False,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    """Drive ``handlers`` in round-robin until stopped or drained.

    Termination:

    * ``once=True`` — make one full pass across all handlers and
      return. If a handler fully drained inside that pass it is *not*
      re-polled — "once" means literally one pass.
    * ``once=False`` — loop forever. When every handler reports
      ``claimed=0`` in a pass, sleep ``idle_seconds`` then re-poll.
    * ``should_stop()`` — called between handlers; returning ``True``
      breaks out cleanly. The CLI wires this to a SIGINT/SIGTERM
      flag so ``Ctrl-C`` returns within one batch.
    """
    if not handlers:
        log.warning("worker: no handlers registered; nothing to do")
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
            log.info(
                "worker: %s claimed=%d ok=%d failed=%d",
                result.handler,
                result.claimed,
                result.ok,
                result.failed,
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


__all__ = ["BatchResult", "run_handler_once", "run_loop"]

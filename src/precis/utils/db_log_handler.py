"""Buffered DB log handler — centralised worker logs in postgres.

Companion to :mod:`logging.FileHandler` (which stays attached as
the bootstrap + fallback channel). This handler writes structured
log records to ``worker_logs`` (migration 0015) so the operator
can query "what did the cluster do in the last hour?" across hosts
without ssh-and-grep.

Discipline
==========

* **Dedicated connection.** A separate psycopg connection in
  autocommit mode. Logging mustn't compete with the worker's main
  pool — a flush stall during a write spike would back up the
  rotation.
* **Buffered with two flush triggers.** The handler buffers
  records in-process. A background daemon thread wakes every
  second; it flushes when either ``MAX_BUFFER`` records have
  accumulated OR ``MAX_INTERVAL_SECONDS`` has elapsed since the
  last flush. Both knobs are env-overridable.
* **Failure → file fallback.** When a flush raises, the buffered
  records are forwarded to whatever stdlib handlers are still
  attached (the file handler) at WARNING level so the operator
  sees the demotion. The handler keeps trying on the next tick;
  no exponential backoff (psycopg's reconnect logic is fine).
* **Bounded buffer.** When the buffer hits 10× ``MAX_BUFFER`` (a
  signal that flushes are failing for a while), the oldest entries
  drop on the floor with a one-time WARNING. This prevents
  unbounded memory growth during a DB outage.
* **Atexit flush.** Registered so a clean shutdown writes the
  tail of the buffer before the process dies.

Identity
========

Each row carries:

* ``host`` — ``PRECIS_HOST_NAME`` env, falls back to
  ``socket.gethostname()``. The fallback is what shows up locally
  ("melchior.local", "caspar"); the env lets containers override.
* ``process`` — ``PRECIS_PROCESS`` env, NULL when unset.
  LaunchDaemon plists set this to "precis-worker" /
  "precis-worker-agent" / "precis-cron-tick" so cross-process
  queries are trivial.
* ``pass`` — derived from the logger name. When the logger matches
  ``precis.workers.<X>``, ``pass`` becomes ``<X>``. NULL otherwise.
  No threadlocal / contextvar plumbing — the logger name is
  already what the caller picked.

Tests live under ``tests/test_db_log_handler.py`` and use a
mocked connection so the buffer/flush logic doesn't need a live
postgres for the unit path.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import socket
import threading
import time
import traceback
from collections import deque
from collections.abc import Iterable
from typing import Any

import psycopg

log = logging.getLogger(__name__)


# Default buffer + flush knobs. The chosen pair (5 seconds OR 50
# records, whichever first) puts the visible-latency floor at ~5s
# during quiet periods while keeping the commit count bounded
# during bursts.
_DEFAULT_MAX_BUFFER = 50
_DEFAULT_MAX_INTERVAL_SECONDS = 5.0

# Hard upper bound on the buffer size during sustained flush
# failures (DB unreachable). Past this, the OLDEST records drop.
# Pick 10× the soft cap — generous enough to ride out a minute of
# outage at the burst rate without losing the recent context the
# operator cares about.
_BUFFER_HARD_CAP_MULTIPLIER = 10

# Pass-name extraction regex. Matches ``precis.workers.<name>`` and
# captures ``<name>``. Anything else → pass=None.
_PASS_LOGGER_PREFIX = "precis.workers."


def _resolve_host_name() -> str:
    """``PRECIS_HOST_NAME`` env or ``socket.gethostname()`` fallback."""
    raw = os.environ.get("PRECIS_HOST_NAME")
    if raw:
        return raw
    return socket.gethostname()


def _resolve_process_name() -> str | None:
    """``PRECIS_PROCESS`` env or ``None``."""
    raw = os.environ.get("PRECIS_PROCESS")
    return raw or None


def _pass_from_logger(logger_name: str | None) -> str | None:
    """Derive the ``pass`` column from a logger name.

    Returns the suffix after ``precis.workers.`` when present.
    Examples:

    * ``precis.workers.dispatch`` → ``dispatch``
    * ``precis.workers.schedule.worker`` → ``schedule`` (first
      segment after the prefix; nested modules collapse to the
      package's pass name)
    * ``precis.utils.load_gate`` → ``None``
    """
    if not logger_name or not logger_name.startswith(_PASS_LOGGER_PREFIX):
        return None
    rest = logger_name[len(_PASS_LOGGER_PREFIX) :]
    return rest.split(".", 1)[0] if rest else None


class BufferedDBLogHandler(logging.Handler):
    """A logging.Handler that batch-inserts into ``worker_logs``.

    Construct **after** the DSN is known (typically right after
    ``Store.connect()``). The handler opens its own psycopg
    connection in autocommit mode so flushes don't sit inside a
    long-running tx; closing the handler releases the connection.

    Args:
        dsn: postgres DSN. Same shape ``Store.connect`` accepts.
        level: standard ``logging`` level int. Defaults to ``INFO``
            so DEBUG chatter doesn't fill the table.
        max_buffer: flush when buffered records reach this count.
            Default 50.
        max_interval_seconds: flush at least this often even with
            small bursts. Default 5.
        process_name: override the process identity. Defaults to
            ``PRECIS_PROCESS`` env.
        host_name: override the host identity. Defaults to
            ``PRECIS_HOST_NAME`` env or hostname.
    """

    def __init__(
        self,
        dsn: str,
        *,
        level: int = logging.INFO,
        max_buffer: int | None = None,
        max_interval_seconds: float | None = None,
        process_name: str | None = None,
        host_name: str | None = None,
    ) -> None:
        super().__init__(level=level)
        self._dsn = dsn
        self._max_buffer = max_buffer or int(
            os.environ.get("PRECIS_LOG_MAX_BUFFER", _DEFAULT_MAX_BUFFER)
        )
        self._max_interval = max_interval_seconds or float(
            os.environ.get(
                "PRECIS_LOG_MAX_INTERVAL_SECONDS", _DEFAULT_MAX_INTERVAL_SECONDS
            )
        )
        self._hard_cap = self._max_buffer * _BUFFER_HARD_CAP_MULTIPLIER
        self._host = host_name or _resolve_host_name()
        self._process = (
            process_name if process_name is not None else _resolve_process_name()
        )
        self._buffer: deque[tuple[Any, ...]] = deque()
        self._lock = threading.Lock()
        self._last_flush_at = time.monotonic()
        self._conn: psycopg.Connection | None = None
        self._dropped_warned = False
        self._stop = threading.Event()
        # Background flush ticker. Daemon thread so it doesn't keep
        # the process alive past a normal exit; the atexit hook
        # below handles the final flush before the daemon dies.
        self._ticker = threading.Thread(
            target=self._run_ticker,
            name="precis-log-flusher",
            daemon=True,
        )
        self._ticker.start()
        atexit.register(self._atexit_flush)

    # ── logging.Handler surface ────────────────────────────────────

    def emit(self, record: logging.LogRecord) -> None:
        """Buffer ``record`` and trigger a size-driven flush if needed.

        Never raises — ``handleError`` swallows so a buggy log call
        from a worker pass can't crash the rotation.
        """
        try:
            row = self._record_to_row(record)
            with self._lock:
                self._buffer.append(row)
                # Hard-cap trim. Drop OLDEST records — the recent
                # ones are more likely to be what the operator needs
                # when they're investigating a current incident.
                while len(self._buffer) > self._hard_cap:
                    self._buffer.popleft()
                    if not self._dropped_warned:
                        self._dropped_warned = True
                        # Use the stdlib handler chain so the warning
                        # lands in the file even if the DB is down.
                        logging.getLogger("precis.db_log_handler").warning(
                            "BufferedDBLogHandler: buffer hard cap "
                            "%d hit; dropping oldest records",
                            self._hard_cap,
                        )
                size = len(self._buffer)
            if size >= self._max_buffer:
                self._flush_or_demote()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        """Stop the ticker, flush remaining records, release the conn."""
        self._stop.set()
        # Let the ticker notice the stop flag before flushing — it
        # holds the lock briefly per tick.
        if self._ticker.is_alive():
            self._ticker.join(timeout=2.0)
        self._flush_or_demote()
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        super().close()

    # ── internals ──────────────────────────────────────────────────

    def _record_to_row(self, record: logging.LogRecord) -> tuple[Any, ...]:
        """Turn a LogRecord into the tuple we'll pass to INSERT."""
        # ``record.exc_info`` is the (type, val, tb) tuple when
        # ``log.exception(...)`` was called. Render to the payload
        # so exception rows carry the traceback as structured data.
        payload: dict[str, Any] | None = None
        extra_payload = getattr(record, "payload", None)
        if isinstance(extra_payload, dict):
            payload = dict(extra_payload)
        if record.exc_info:
            payload = payload or {}
            exc_type, exc_val, exc_tb = record.exc_info
            payload["error_class"] = exc_type.__name__ if exc_type else "Unknown"
            payload["error_msg"] = (str(exc_val) or "")[:500]
            tb_str = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))
            payload["traceback"] = tb_str[:2000]
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        # Truncate the message to keep row size bounded. 4KB is
        # plenty for ops chatter; longer messages indicate a misuse
        # of log.info() to carry data that should go in payload.
        if len(message) > 4000:
            message = message[:4000] + "…"
        return (
            self._host,
            self._process,
            _pass_from_logger(record.name),
            record.levelname,
            record.name,
            message,
            json.dumps(payload, default=str) if payload else None,
        )

    def _run_ticker(self) -> None:
        """Background loop — flush when the time threshold trips."""
        while not self._stop.wait(timeout=1.0):
            try:
                elapsed = time.monotonic() - self._last_flush_at
                if elapsed >= self._max_interval and self._buffer:
                    self._flush_or_demote()
            except Exception:
                # Defensive: a ticker exception mustn't kill the
                # thread and silently stop log flushing.
                logging.getLogger("precis.db_log_handler").exception(
                    "BufferedDBLogHandler ticker tick failed"
                )

    def _flush_or_demote(self) -> None:
        """Atomic swap-out of the buffer; INSERT; on failure, demote."""
        with self._lock:
            if not self._buffer:
                return
            batch = list(self._buffer)
            self._buffer.clear()
            self._last_flush_at = time.monotonic()
        try:
            self._do_insert(batch)
        except Exception:
            self._demote_batch_to_file(batch)

    def _do_insert(self, batch: Iterable[tuple[Any, ...]]) -> None:
        """Open the dedicated conn lazily; executemany the batch."""
        if self._conn is None or getattr(self._conn, "closed", True):
            self._conn = psycopg.connect(self._dsn, autocommit=True)
        # Same INSERT for every row. The (ts) default is now() so
        # we don't pass it explicitly — letting the server stamp
        # the time keeps cross-host clock drift from showing up
        # in ordering.
        self._conn.cursor().executemany(
            """
            INSERT INTO worker_logs
                (host, process, pass, level, logger, message, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            list(batch),
        )

    def _demote_batch_to_file(self, batch: Iterable[tuple[Any, ...]]) -> None:
        """When the DB flush fails, re-log the batch via the stdlib
        chain at WARNING so the file handler captures them.

        Also a one-shot WARNING so the operator notices the demotion.
        """
        fallback_logger = logging.getLogger("precis.db_log_handler")
        # Use a sentinel attribute to suppress the demote-warning
        # being itself buffered + demoted in an infinite loop.
        if not getattr(self, "_demote_warned_recently", False):
            self._demote_warned_recently = True
            fallback_logger.warning(
                "BufferedDBLogHandler: DB flush failed; demoting "
                "%d records to file handler",
                len(list(batch)) if isinstance(batch, list) else 1,
            )
        # Re-format each row as a single line so it's grep-friendly.
        for host, process, pass_, level, logger_name, message, payload in batch:
            fallback_logger.log(
                logging.WARNING,
                "[%s host=%s process=%s pass=%s logger=%s] %s%s",
                level,
                host,
                process or "-",
                pass_ or "-",
                logger_name or "-",
                message,
                f" payload={payload}" if payload else "",
            )

    def _atexit_flush(self) -> None:
        """Final flush before the process dies."""
        try:
            self._flush_or_demote()
        except Exception:
            # We're in atexit; nothing useful to do with an error
            # except not crash the process exit path.
            pass


__all__ = ["BufferedDBLogHandler", "_pass_from_logger", "_resolve_host_name"]

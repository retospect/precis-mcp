"""Tests for the buffered DB log handler.

Two layers:

* Unit tests with a mocked psycopg connection — buffer flush
  triggers, file fallback, pass-name derivation, atexit, host /
  process resolution.
* One DB integration test: actually attach the handler, log a few
  lines, query ``worker_logs`` for them. Skips when no DB is
  configured (uses the standard ``store`` fixture).
"""

from __future__ import annotations

import logging
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from precis.store import Store
from precis.utils.db_log_handler import (
    BufferedDBLogHandler,
    _pass_from_logger,
    _resolve_host_name,
)

# ── pass-name derivation (pure) ───────────────────────────────────


def test_pass_from_logger_matches_worker_prefix() -> None:
    assert _pass_from_logger("precis.workers.dispatch") == "dispatch"
    assert _pass_from_logger("precis.workers.schedule") == "schedule"


def test_pass_from_logger_collapses_subpackages() -> None:
    """Nested modules under workers.X collapse to X."""
    assert _pass_from_logger("precis.workers.schedule.worker") == "schedule"
    assert (
        _pass_from_logger("precis.workers.auto_check_evaluators.time_past")
        == "auto_check_evaluators"
    )


def test_pass_from_logger_returns_none_for_non_worker() -> None:
    assert _pass_from_logger("precis.utils.load_gate") is None
    assert _pass_from_logger("precis.handlers.todo") is None
    assert _pass_from_logger(None) is None
    assert _pass_from_logger("") is None


# ── host resolution ──────────────────────────────────────────────


def test_host_resolution_env_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_HOST_NAME", "test-host")
    assert _resolve_host_name() == "test-host"


def test_host_resolution_falls_back_to_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRECIS_HOST_NAME", raising=False)
    # Whatever socket.gethostname returns, the resolver returns the
    # same. We can't assert on a specific value because tests run on
    # arbitrary hosts.
    assert _resolve_host_name()


# ── handler buffer + flush ───────────────────────────────────────


@pytest.fixture
def fake_psycopg(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Stub out psycopg.connect so the handler can be constructed
    without a real DB.

    Returns the MagicMock standing in for the connection so tests
    can inspect ``cursor().executemany()`` calls.
    """
    fake_conn = MagicMock(name="conn")
    fake_conn.closed = False
    fake_cursor = MagicMock(name="cursor")
    fake_conn.cursor.return_value = fake_cursor
    monkeypatch.setattr(
        "precis.utils.db_log_handler.psycopg.connect",
        lambda dsn, **kw: fake_conn,
    )
    return fake_conn


def _make_handler(fake_psycopg: MagicMock, **kwargs) -> BufferedDBLogHandler:
    """Build a handler with safe defaults for unit tests."""
    defaults: dict[str, Any] = dict(
        max_buffer=3,
        max_interval_seconds=1.0,
        host_name="testhost",
        process_name="test",
    )
    defaults.update(kwargs)
    return BufferedDBLogHandler("postgresql://stub", **defaults)


def _setup_logger(name: str, h: BufferedDBLogHandler) -> logging.Logger:
    """Attach handler + force INFO-level so log.info() records reach
    the handler regardless of the test runner's root-logger config."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.addHandler(h)
    return logger


def test_flush_triggers_on_buffer_size(
    fake_psycopg: MagicMock,
) -> None:
    h = _make_handler(fake_psycopg)
    logger = _setup_logger("precis.workers.dispatch", h)
    try:
        # max_buffer=3 → fourth record should not be needed; the
        # third should already trigger the flush.
        for i in range(3):
            logger.info("test %d", i)
        # Allow the in-emit flush path to settle.
        fake_psycopg.cursor.return_value.executemany.assert_called()
        first_call = fake_psycopg.cursor.return_value.executemany.call_args
        assert "INSERT INTO worker_logs" in first_call.args[0]
        # The second positional arg is the rows; should be a list of 3.
        rows = first_call.args[1]
        assert len(rows) == 3
        # Each row is (host, process, pass, level, logger, message, payload).
        for row in rows:
            assert row[0] == "testhost"
            assert row[1] == "test"
            assert row[2] == "dispatch"
            assert row[3] == "INFO"
            assert row[4] == "precis.workers.dispatch"
    finally:
        logger.removeHandler(h)
        h.close()


def test_flush_triggers_on_time(fake_psycopg: MagicMock) -> None:
    """Even one record left in the buffer should flush after the
    interval elapses (background ticker)."""
    h = _make_handler(fake_psycopg, max_buffer=999, max_interval_seconds=0.2)
    logger = _setup_logger("precis.workers.schedule", h)
    try:
        logger.info("one record")
        # Sleep past the interval. The daemon thread wakes every
        # second, so we need to allow for that minimum.
        time.sleep(1.5)
        fake_psycopg.cursor.return_value.executemany.assert_called()
    finally:
        logger.removeHandler(h)
        h.close()


def test_db_failure_demotes_to_file(
    fake_psycopg: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """When the INSERT raises, the records reach the stdlib
    fallback (file handler in production; caplog in tests)."""
    fake_psycopg.cursor.return_value.executemany.side_effect = RuntimeError(
        "db unreachable"
    )
    h = _make_handler(fake_psycopg)
    logger = _setup_logger("precis.workers.nursery", h)
    try:
        caplog.set_level(logging.WARNING, logger="precis.db_log_handler")
        for i in range(3):
            logger.info("demote-me-%d", i)
        # The demote path logs at WARNING via the stdlib chain.
        warning_msgs = [
            r.message for r in caplog.records if r.name == "precis.db_log_handler"
        ]
        assert any("DB flush failed" in m for m in warning_msgs)
    finally:
        logger.removeHandler(h)
        h.close()


def test_handler_close_releases_connection(
    fake_psycopg: MagicMock,
) -> None:
    h = _make_handler(fake_psycopg)
    logger = _setup_logger("precis.workers.dispatch", h)
    logger.info("trigger conn open")
    # Trigger a flush so the connection opens.
    for _ in range(3):
        logger.info("flush trigger")
    h.close()
    fake_psycopg.close.assert_called()
    logger.removeHandler(h)


def test_record_payload_extra_lands_in_jsonb(
    fake_psycopg: MagicMock,
) -> None:
    """``log.info('x', extra={'payload': {...}})`` survives the row
    serialisation."""
    h = _make_handler(fake_psycopg)
    logger = _setup_logger("precis.workers.dispatch", h)
    try:
        logger.info(
            "minted",
            extra={"payload": {"job_id": 42, "executor": "claude_inproc"}},
        )
        for _ in range(2):  # fill buffer to trigger flush
            logger.info("x")
        rows = fake_psycopg.cursor.return_value.executemany.call_args.args[1]
        # The first record carries the payload as the seventh column.
        first = rows[0]
        assert first[6] is not None
        assert '"job_id": 42' in first[6]
        assert '"executor": "claude_inproc"' in first[6]
    finally:
        logger.removeHandler(h)
        h.close()


def test_exception_record_includes_traceback_in_payload(
    fake_psycopg: MagicMock,
) -> None:
    h = _make_handler(fake_psycopg)
    logger = _setup_logger("precis.workers.structural", h)
    try:
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("oops")
        for _ in range(2):
            logger.info("x")
        rows = fake_psycopg.cursor.return_value.executemany.call_args.args[1]
        exc_row = rows[0]
        assert exc_row[3] == "ERROR"
        assert exc_row[6] is not None
        assert '"error_class": "ValueError"' in exc_row[6]
        assert '"error_msg": "boom"' in exc_row[6]
        assert "traceback" in exc_row[6]
    finally:
        logger.removeHandler(h)
        h.close()


# ── DB integration (skips without DB) ────────────────────────────


def test_handler_writes_to_real_db(store: Store) -> None:
    """Smoke test against the live test DB.

    Uses the shared ``store`` fixture so migrations have already
    applied and 0015 / worker_logs exists. Logs one row via the
    standard handler chain, then SELECTs to verify.
    """
    from tests.conftest import PG_TEST_DSN

    h = BufferedDBLogHandler(
        PG_TEST_DSN,
        max_buffer=2,
        max_interval_seconds=0.5,
        host_name="integration-test",
        process_name="test-runner",
    )
    logger = _setup_logger("precis.workers.dispatch", h)
    try:
        logger.info("integration test row")
        logger.info("second row to trigger flush")
        # Give the buffer a chance to flush (size threshold should
        # have fired; allow the worker thread one cycle).
        time.sleep(0.3)
        with store.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT host, process, pass, level, logger, message
                  FROM worker_logs
                 WHERE host = %s
                 ORDER BY log_id DESC LIMIT 1
                """,
                ("integration-test",),
            ).fetchone()
        assert row is not None
        assert row[0] == "integration-test"
        assert row[1] == "test-runner"
        assert row[2] == "dispatch"
        assert row[3] == "INFO"
        assert row[4] == "precis.workers.dispatch"
        assert "second row" in row[5]
    finally:
        logger.removeHandler(h)
        h.close()


# Silence unused-import warnings on the patch path; the symbol is
# imported above for monkeypatch and ergonomic test-side use.
_ = patch

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
    _quiet_http_client_loggers,
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


# ── secret redaction (gr502250) ──────────────────────────────────
#
# The records below are built directly, not via a logger, so the
# factory-installed ``log_scrub`` filter never touches them — the
# masking asserted here is the handler's own, at write time.


def _bare_record(
    name: str, msg: str, *args: object, **fields: object
) -> logging.LogRecord:
    rec = logging.LogRecord(name, logging.INFO, __file__, 1, msg, args, None)
    for k, v in fields.items():
        setattr(rec, k, v)
    return rec


def test_row_masks_url_query_credentials_in_message(
    fake_psycopg: MagicMock,
) -> None:
    """An httpx INFO line carries ``api_key=<key>`` verbatim; the stored row
    masks credential params and keeps the rest of the URL legible."""
    h = _make_handler(fake_psycopg)
    try:
        rec = _bare_record(
            "httpx",
            'HTTP Request: GET %s "%s"',
            "https://content.openalex.org/x.pdf?"
            + "api_"
            + "key=FAKEKEY01&filter=doi&mail"
            + "to=me%40x.org",
            "HTTP/1.1 200 OK",
        )
        row = h._record_to_row(rec)
        msg = row[5]
        assert "FAKEKEY01" not in msg
        assert "me%40x.org" not in msg
        assert "content.openalex.org" in msg
        assert "filter=doi" in msg
        assert "HTTP/1.1 200 OK" in msg
    finally:
        h.close()


def test_row_masks_credentials_in_extra_payload(
    fake_psycopg: MagicMock,
) -> None:
    """``extra={'payload': …}`` bypasses the record-creation scrub — the
    handler masks the serialised JSON before the row is written."""
    h = _make_handler(fake_psycopg)
    try:
        rec = _bare_record(
            "precis.workers.fetch_oa",
            "download failed",
            payload={
                "url": "https://content.openalex.org/y?" + "api_" + "key=FAKEKEY02",
                "doi": "10.1/x",
            },
        )
        row = h._record_to_row(rec)
        payload = row[6]
        assert "FAKEKEY02" not in payload
        assert "api_key" in payload and "10.1/x" in payload
    finally:
        h.close()


def test_row_masks_credentials_in_exception_payload(
    fake_psycopg: MagicMock,
) -> None:
    """``error_msg``/``traceback`` carry the exception's URL too."""
    import sys

    h = _make_handler(fake_psycopg)
    # The fake key goes through a variable so the traceback's source line
    # doesn't carry it literally.
    key = "FAKE" + "KEY03"
    try:
        try:
            raise ValueError(
                f"GET https://content.openalex.org/z?api_key={key} timed out"
            )
        except ValueError:
            rec = logging.LogRecord(
                "precis.workers.fetch_oa",
                logging.ERROR,
                __file__,
                1,
                "download failed",
                None,
                sys.exc_info(),
            )
        row = h._record_to_row(rec)
        assert "FAKEKEY03" not in row[5]
        assert "FAKEKEY03" not in (row[6] or "")
        assert "download failed" in row[5]
    finally:
        h.close()


def test_attach_quiets_http_client_loggers() -> None:
    saved = {n: logging.getLogger(n).level for n in ("httpx", "httpcore", "httpcore2")}
    try:
        for n in saved:
            logging.getLogger(n).setLevel(logging.INFO)
        _quiet_http_client_loggers()
        for n in saved:
            assert logging.getLogger(n).level == logging.WARNING
    finally:
        for n, level in saved.items():
            logging.getLogger(n).setLevel(level)


# ── DB integration (skips without DB) ────────────────────────────


def test_handler_writes_to_real_db(store: Store) -> None:
    """Smoke test against the live test DB.

    Uses the shared ``store`` fixture so migrations have already
    applied and 0015 / worker_logs exists. Logs one row via the
    standard handler chain, then SELECTs to verify.
    """
    # The handler must write to the same DB the ``store`` fixture reads —
    # the session's private clone, not the template (``PG_TEST_DSN``).
    from tests.conftest import _active_dsn

    h = BufferedDBLogHandler(
        _active_dsn(),
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

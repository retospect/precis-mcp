"""Bearer tokens stay in requests, but never in Uvicorn access records."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from uvicorn import Config
from uvicorn.logging import AccessFormatter
from uvicorn.protocols.http.h11_impl import H11Protocol
from uvicorn.server import ServerState

from precis.utils import log_scrub
from precis_web.access_log import AccessTokenFilter
from precis_web.app import create_app
from precis_web.config import WebConfig


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    factory = logging.getLogRecordFactory()
    loggers = [
        logging.getLogger(name)
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
    ]
    states = [
        (logger, logger.handlers[:], logger.level, logger.propagate, logger.disabled)
        for logger in loggers
    ]
    try:
        yield
    finally:
        logging.setLogRecordFactory(factory)
        for logger, handlers, level, propagate, disabled in states:
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate
            logger.disabled = disabled


def _record(
    message: str, args: tuple[object, ...] = (), *, name: str = "uvicorn.access"
) -> logging.LogRecord:
    return logging.LogRecord(name, logging.INFO, __file__, 1, message, args, None)


def test_rendered_request_line_redacts_bearer() -> None:
    record = _record("GET /files/pdf/abc?t=v1.get.x HTTP/1.1")
    assert AccessTokenFilter().filter(record)
    assert record.getMessage() == "GET /files/pdf/abc?t=REDACTED HTTP/1.1"


@pytest.mark.parametrize(
    "query", ["t=first&t=second", "%74=first&t=second", "t=&%74=second"]
)
def test_all_token_values_redacted_without_reencoding(query: str) -> None:
    record = _record(f"GET /files/pdf/abc?keep=a%20b&{query}&last=%2F HTTP/1.1")
    AccessTokenFilter().filter(record)
    expected = (
        query.replace("first", "REDACTED")
        .replace("second", "REDACTED")
        .replace("t=&", "t=REDACTED&")
    )
    assert (
        record.getMessage()
        == f"GET /files/pdf/abc?keep=a%20b&{expected}&last=%2F HTTP/1.1"
    )


def test_uvicorn_formatter_retains_five_arguments() -> None:
    record = _record(
        '%s - "%s %s HTTP/%s" %d',
        ("client", "GET", "/files/pdf/abc?t=v1.get.x&n=2", "1.1", 200),
    )
    AccessTokenFilter().filter(record)
    assert record.args == ("client", "GET", "/files/pdf/abc?t=REDACTED&n=2", "1.1", 200)
    rendered = AccessFormatter(
        fmt="%(request_line)s %(status_code)s", use_colors=False
    ).format(record)
    assert "GET /files/pdf/abc?t=REDACTED&n=2 HTTP/1.1" in rendered
    assert "200" in rendered


@pytest.mark.parametrize("token", ["value#suffix", 'value"suffix', "value suffix"])
def test_structured_target_redacts_the_entire_value(token: str) -> None:
    record = _record(
        '%s - "%s %s HTTP/%s" %d',
        ("client", "GET", f"/files/pdf/abc?t={token}&keep=visible", "1.1", 200),
    )
    AccessTokenFilter().filter(record)
    assert record.args == (
        "client",
        "GET",
        "/files/pdf/abc?t=REDACTED&keep=visible",
        "1.1",
        200,
    )


@pytest.mark.parametrize(
    "name,target",
    [
        ("uvicorn.access", "/files/pdf/abc?title=t%3Dkeep&T=keep"),
        ("uvicorn.access", "/files/pdf/abc"),
        ("application", "/files/pdf/abc?t=keep"),
    ],
)
def test_unrelated_records_unchanged(name: str, target: str) -> None:
    record = _record("GET %s HTTP/1.1", (target,), name=name)
    original = (record.msg, record.args)
    AccessTokenFilter().filter(record)
    assert (record.msg, record.args) == original


@pytest.mark.parametrize("token", ["v1.get.x", "ghp_" + "a" * 36])
def test_existing_factory_and_secret_scrub_are_chained(
    monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    previous = logging.getLogRecordFactory()
    calls: list[logging.LogRecord] = []

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        calls.append(record)
        return record

    logging.setLogRecordFactory(factory)
    monkeypatch.setattr(log_scrub, "_installed", False)
    log_scrub.install_log_scrub()
    create_app(runtime=SimpleNamespace(), web_config=WebConfig(auth_required=False))
    installed = logging.getLogRecordFactory()
    create_app(runtime=SimpleNamespace(), web_config=WebConfig(auth_required=False))
    assert logging.getLogRecordFactory() is installed
    record = logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("client", "GET", f"/files/pdf/abc?t={token}", "1.1", 200),
        None,
    )
    assert record in calls
    assert record.args == ("client", "GET", "/files/pdf/abc?t=REDACTED", "1.1", 200)
    assert "REDACTED" in AccessFormatter(use_colors=False).format(record)


@pytest.mark.parametrize(
    "factory_first",
    [False, True],
    ids=["app-before-config", "config-before-factory-app"],
)
@pytest.mark.parametrize("path", ["/podcast/feed.xml", "/files/pdf/abc"])
def test_real_uvicorn_access_records_redacted(
    caplog: pytest.LogCaptureFixture, factory_first: bool, path: str
) -> None:
    expected_status = 403 if path.startswith("/files/") else 200

    def build() -> FastAPI:
        # The route requires a store reference, but malformed tokens must be
        # rejected before any store operation; this object cannot perform one.
        return create_app(
            runtime=SimpleNamespace(store=object()),
            web_config=WebConfig(auth_required=False),
        )

    async def exercise_request() -> None:
        config = Config(build, factory=True) if factory_first else Config(build())
        # Config's default dictConfig replaces access handlers. Capture only after it.
        access = logging.getLogger("uvicorn.access")
        access.addHandler(caplog.handler)
        access.setLevel(logging.INFO)
        state = ServerState()
        protocol = H11Protocol(config=config, server_state=state, app_state={})
        transport = Mock(spec=asyncio.Transport)
        transport.get_extra_info.return_value = None
        transport.is_closing.return_value = False
        protocol.connection_made(transport)
        try:
            protocol.data_received(
                f"GET {path}?t=v1.get.x&keep=visible HTTP/1.0\r\n\r\n".encode()
            )
            await asyncio.wait_for(asyncio.gather(*state.tasks), timeout=5)
            records = [
                record for record in caplog.records if record.name == "uvicorn.access"
            ]
            assert len(records) == 1
            record = records[0]
            assert record.args == (
                "",
                "GET",
                f"{path}?t=REDACTED&keep=visible",
                "1.0",
                expected_status,
            )
            assert "v1.get.x" not in record.getMessage()
            assert "REDACTED" in AccessFormatter(use_colors=False).format(record)
            wire = b"".join(call.args[0] for call in transport.write.call_args_list)
            if path.startswith("/files/"):
                assert b"403 Forbidden" in wire
                assert b'"detail":"invalid token"' in wire
            else:
                assert b"200 OK" in wire
                assert b"<rss" in wire
        finally:
            protocol.connection_lost(None)

    asyncio.run(exercise_request())

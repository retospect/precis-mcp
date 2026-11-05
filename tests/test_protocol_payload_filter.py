"""Transport logging preserves diagnostics while redacting wire payloads."""

from __future__ import annotations

import asyncio
import io
import logging
import sys
import traceback
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared import session as sdk_session
from mcp.types import JSONRPCMessage
from pydantic import BaseModel, ValidationError, field_validator
from pydantic_core import PydanticCustomError
from rich.console import Console
from rich.logging import RichHandler

from precis import server
from precis.server import _PROTOCOL_PAYLOAD_FILTER, _PROTOCOL_SESSION_FILTER

_SECRET = "private-learning-bearer-sentinel"


def _parse_private_message() -> None:
    JSONRPCMessage.model_validate({"jsonrpc": "2.0", "id": 1, "method": [_SECRET]})


def _transport_failure() -> None:
    raise RuntimeError("transport failed")


def _parser_record(*, nested: str = "none") -> logging.LogRecord:
    try:
        try:
            _parse_private_message()
        except ValidationError as exc:
            if nested == "cause":
                raise RuntimeError("transport failed") from exc
            if nested == "context":
                _transport_failure()
            if nested == "group":
                raise ExceptionGroup("transport failed", [exc]) from None
            raise
    except Exception:
        return logging.LogRecord(
            "mcp.server.sse",
            logging.ERROR,
            __file__,
            1,
            "Failed to parse message",
            (),
            sys.exc_info(),
        )
    raise AssertionError("Expected parser failure")


@pytest.mark.parametrize("nested", ["none", "cause", "context", "group"])
def test_parser_failure_retains_traceback_without_input(nested: str) -> None:
    record = _parser_record(nested=nested)
    formatter = logging.Formatter("%(levelname)s %(message)s")
    # Some other handler may already have cached the unsanitized exception.
    assert _SECRET in formatter.format(record)

    assert _PROTOCOL_PAYLOAD_FILTER.filter(record)

    rendered = formatter.format(record)
    assert record.exc_info is None
    assert "Failed to parse message" in rendered
    assert "ValidationError" in rendered
    assert "Traceback (most recent call last)" in rendered
    assert "_parse_private_message" in rendered
    assert "JSONRPCRequest.method" in rendered
    assert "Input should be a valid string" in rendered
    assert _SECRET not in rendered
    assert "input_value=" not in rendered


@pytest.mark.parametrize("args", [(_SECRET,), ({"payload": _SECRET},)])
def test_arguments_are_redacted_and_format_is_rendered(args) -> None:
    message = "Received %(payload)s" if isinstance(args[0], dict) else "Received %s"
    record = logging.LogRecord(
        "mcp.server.lowlevel.server", logging.INFO, __file__, 1, message, args, None
    )

    assert _PROTOCOL_PAYLOAD_FILTER.filter(record)

    assert record.msg == "Received [redacted]"
    assert record.args == ()
    assert logging.Formatter().format(record) == "Received [redacted]"


def test_debug_payload_is_dropped() -> None:
    record = logging.LogRecord(
        "sse_starlette.sse", logging.DEBUG, __file__, 1, f"chunk: {_SECRET}", (), None
    )
    assert not _PROTOCOL_PAYLOAD_FILTER.filter(record)


def test_plain_exception_preserves_full_error_and_stack() -> None:
    try:
        raise RuntimeError("socket closed while sending response; retry exhausted")
    except RuntimeError:
        record = logging.LogRecord(
            "mcp.server.streamable_http",
            logging.ERROR,
            __file__,
            1,
            "Error in SSE writer",
            (),
            sys.exc_info(),
            sinfo="Stack (most recent call last):\n  transport writer",
        )

    formatter = logging.Formatter()
    original = formatter.format(record)
    assert _PROTOCOL_PAYLOAD_FILTER.filter(record)
    assert formatter.format(record) == original


def test_lowlevel_preformatted_parser_error_omits_input() -> None:
    parser = _parser_record()
    assert parser.exc_info is not None
    record = logging.LogRecord(
        "mcp.server.lowlevel.server",
        logging.ERROR,
        __file__,
        1,
        f"Received exception from stream: {parser.exc_info[1]}",
        (),
        None,
    )

    assert _PROTOCOL_PAYLOAD_FILTER.filter(record)

    rendered = logging.Formatter().format(record)
    assert "Received exception from stream:" in rendered
    assert "JSONRPCRequest.method" in rendered
    assert "Input should be a valid string" in rendered
    assert _SECRET not in rendered


def test_sse_preformatted_invalid_session_omits_client_query() -> None:
    record = logging.LogRecord(
        "mcp.server.sse",
        logging.WARNING,
        __file__,
        1,
        f"Received invalid session ID: {_SECRET}",
        (),
        None,
    )
    assert _PROTOCOL_PAYLOAD_FILTER.filter(record)
    assert (
        logging.Formatter().format(record) == "Received invalid session ID: [redacted]"
    )


def test_sse_unknown_session_omits_client_uuid() -> None:
    client_id = "3f6d85c2-129e-4a1d-a2b0-a0b32e2d5642"
    record = logging.LogRecord(
        "mcp.server.sse",
        logging.WARNING,
        __file__,
        1,
        f"Could not find session for ID: {client_id}",
        (),
        None,
    )
    assert _PROTOCOL_PAYLOAD_FILTER.filter(record)
    rendered = logging.Formatter().format(record)
    assert rendered == "Could not find session for ID: [redacted]"
    assert client_id not in rendered


@pytest.mark.parametrize("shape", ["bearer", "github", "query", "personal"])
def test_real_secret_scrub_and_protocol_filter_preserve_masked_traceback(
    monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    from precis.mcp_identity import TOKEN_PREFIX
    from precis.utils import log_scrub

    # Restore both the factory and installation flag when the test exits.
    monkeypatch.setattr(logging, "_logRecordFactory", logging.getLogRecordFactory())
    monkeypatch.setattr(log_scrub, "_installed", False)
    log_scrub.install_log_scrub()
    secret = "a1B2" * 9
    if shape == "bearer":
        detail = f"Authorization: Bearer {secret}"
    elif shape == "github":
        secret = "ghp_" + secret
        detail = secret
    elif shape == "query":
        detail = f"https://example.invalid/x?apiKey={secret}"
    else:
        secret = TOKEN_PREFIX + "a" * 43
        detail = secret
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("mcp.server.streamable_http")
    monkeypatch.setattr(logger, "handlers", [handler])
    monkeypatch.setattr(logger, "filters", [_PROTOCOL_PAYLOAD_FILTER])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(logger, "level", logging.ERROR)

    try:
        raise RuntimeError(f"upstream failed: {detail}")
    except RuntimeError:
        logger.exception("Error in SSE writer: " + detail)

    rendered = output.getvalue()
    assert secret not in rendered
    assert "RuntimeError" in rendered
    assert "Traceback (most recent call last)" in rendered
    assert "upstream failed:" in rendered


def test_direct_exception_reader_cannot_recover_private_input() -> None:
    class DirectExceptionHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.raw = ""
            self.rendered = ""

        def emit(self, record: logging.LogRecord) -> None:
            if record.exc_info:
                self.raw = "".join(traceback.format_exception(*record.exc_info))
            self.rendered = self.format(record)

    record = _parser_record()
    assert _PROTOCOL_PAYLOAD_FILTER.filter(record)
    handler = DirectExceptionHandler()
    handler.handle(record)
    assert handler.raw == ""
    assert _SECRET not in handler.rendered
    assert "ValidationError" in handler.rendered
    assert "Traceback (most recent call last)" in handler.rendered


def test_rich_handler_displays_sanitized_plain_traceback() -> None:
    output = io.StringIO()
    handler = RichHandler(
        console=Console(file=output, width=200, color_system=None),
        rich_tracebacks=True,
        show_time=False,
        show_path=False,
    )
    record = _parser_record()
    assert _PROTOCOL_PAYLOAD_FILTER.filter(record)
    handler.handle(record)
    rendered = output.getvalue()
    assert _SECRET not in rendered
    assert "ValidationError" in rendered
    assert "Traceback (most recent call last)" in rendered
    assert "JSONRPCRequest.method" in rendered


@pytest.mark.parametrize(
    "error_kind", ["private_custom", "value_error", "assertion_error"]
)
def test_validator_error_messages_cannot_embed_private_input(error_kind: str) -> None:
    class PrivateModel(BaseModel):
        value: int

        @field_validator("value", mode="before")
        @classmethod
        def reject_value(cls, value: Any) -> Any:
            if error_kind == "value_error":
                raise ValueError(f"bad private value {value}")
            if error_kind == "assertion_error":
                raise AssertionError(f"bad private value {value}")
            raise PydanticCustomError(
                "private_custom", "bad private value {value}", {"value": value}
            )

    try:
        PrivateModel.model_validate({"value": _SECRET})
    except ValidationError:
        record = logging.LogRecord(
            "mcp.server.sse",
            logging.ERROR,
            __file__,
            1,
            "Parser failed",
            (),
            sys.exc_info(),
        )
    else:
        raise AssertionError("Expected validator failure")

    assert _PROTOCOL_PAYLOAD_FILTER.filter(record)
    rendered = logging.Formatter().format(record)
    assert "ValidationError" in rendered
    assert "PrivateModel" in rendered
    assert "value" in rendered
    assert error_kind in rendered
    assert "Traceback (most recent call last)" in rendered
    assert _SECRET not in rendered
    assert "bad private value" not in rendered
    assert "input_value=" not in rendered


def test_internal_filter_failure_keeps_safe_exception_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_render(exc: BaseException) -> traceback.TracebackException:
        raise RuntimeError(_SECRET)

    record = _parser_record()
    # The fallback must also replace an already cached unsafe traceback.
    assert _SECRET in logging.Formatter().format(record)
    monkeypatch.setattr(server, "_protocol_exception_trace", fail_render)
    assert _PROTOCOL_PAYLOAD_FILTER.filter(record)
    rendered = logging.Formatter().format(record)
    assert "ValidationError" in rendered
    assert "(details unavailable)" in rendered
    assert _SECRET not in rendered
    assert record.exc_info is None


@pytest.mark.parametrize(
    ("logger_name", "template"),
    [
        (
            "mcp.server.streamable_http_manager",
            "Created new transport with session ID: {}",
        ),
        ("mcp.server.streamable_http", "Terminating session: {}"),
        ("mcp.server.streamable_http_manager", "Session {} idle timeout"),
        ("mcp.server.streamable_http_manager", "Session {} crashed"),
    ],
)
def test_preformatted_session_ids_are_redacted(logger_name: str, template: str) -> None:
    record = logging.LogRecord(
        logger_name, logging.INFO, __file__, 1, template.format(_SECRET), (), None
    )
    assert _PROTOCOL_PAYLOAD_FILTER.filter(record)
    assert logging.Formatter().format(record) == template.format("[redacted]")


@pytest.mark.parametrize(
    ("message", "args", "expected"),
    [
        (
            "Refusing to open a new session: %d sessions",
            (42,),
            "Refusing to open a new session: 42 sessions",
        ),
        ("Received %s at %d", (_SECRET, 4), "Received [redacted] at 4"),
        (
            "Received %(payload)s at %(count)d",
            ({"payload": _SECRET, "count": 4},),
            "Received [redacted] at 4",
        ),
        ("Received %d", (_SECRET,), "Received %d"),
    ],
)
def test_arguments_keep_numbers_and_guard_invalid_formats(
    message: str, args: tuple[Any, ...], expected: str
) -> None:
    record = logging.LogRecord(
        "mcp.server.lowlevel.server", logging.WARNING, __file__, 1, message, args, None
    )
    assert _PROTOCOL_PAYLOAD_FILTER.filter(record)
    assert record.args == ()
    assert logging.Formatter().format(record) == expected


def test_root_filter_leaves_other_sources_untouched() -> None:
    record = _parser_record()
    record.name = "root"
    record.msg = f"Message was: {_SECRET}"
    record.args = ()
    before = record.__dict__.copy()
    assert _PROTOCOL_SESSION_FILTER.filter(record)
    assert record.__dict__ == before


def test_root_filter_drops_sdk_debug_payload() -> None:
    record = logging.LogRecord(
        "root",
        logging.DEBUG,
        sdk_session.BaseSession._receive_loop.__code__.co_filename,
        1,
        f"Message that failed validation: {_SECRET}",
        (),
        None,
    )
    assert not _PROTOCOL_SESSION_FILTER.filter(record)


def test_root_filter_redacts_preformatted_response_id() -> None:
    record = logging.LogRecord(
        "root",
        logging.WARNING,
        sdk_session.BaseSession._receive_loop.__code__.co_filename,
        1,
        f"Response ID {_SECRET!r} cannot be normalized to match pending requests",
        (),
        None,
    )
    assert _PROTOCOL_SESSION_FILTER.filter(record)
    assert record.getMessage() == (
        "Response ID [redacted] cannot be normalized to match pending requests"
    )


def test_sdk_request_and_notification_validation_logs_are_private(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    mcp = FastMCP("protocol-log-test", json_response=True)
    monkeypatch.setattr(server, "mcp", mcp)
    server._register_tools_from_registry()
    server._install_protocol_payload_filters()
    app = mcp.streamable_http_app()
    server._install_token_auth(app, token="install", store=None)
    caplog.set_level(logging.DEBUG)

    async def exercise() -> None:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
            ) as client,
        ):
            headers = {
                "Authorization": "Bearer install",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            }
            initialized = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
            )
            assert initialized.status_code == 200
            headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]
            headers["Mcp-Protocol-Version"] = "2025-03-26"
            await client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            malformed = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": [_SECRET],
                        "arguments": {"kind": "learning", "note": _SECRET},
                    },
                },
            )
            assert malformed.status_code == 200
            assert malformed.json()["error"]["code"] == -32602
            notification = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {
                        "requestId": {"private": _SECRET},
                    },
                },
            )
            assert notification.status_code == 202

    asyncio.run(exercise())
    warnings = [
        record
        for record in caplog.records
        if record.name == "root"
        and record.getMessage().startswith("Failed to validate")
    ]
    assert any(
        "Failed to validate request:" in record.getMessage() for record in warnings
    )
    assert any(
        "Failed to validate notification:" in record.getMessage() for record in warnings
    )
    formatter = logging.Formatter()
    for record in caplog.records:
        assert _SECRET not in formatter.format(record)
    for record in warnings:
        assert "input_value=" not in record.getMessage()
        assert "Message was:" not in record.getMessage()


@pytest.mark.parametrize("method", ["prompts/get", "completion/complete"])
@pytest.mark.parametrize(
    "key",
    [
        "private-argument-key",
        "private-argument-key\nWARNING forged entry",
        "private-argument-key\rWARNING forged entry",
        "private-argument-key\x1b[2J",
        "private-argument-key\u2028WARNING forged entry",
    ],
    ids=["plain", "newline", "carriage-return", "terminal-escape", "unicode-line"],
)
def test_sdk_validation_warning_omits_client_argument_keys(
    method: str, key: str
) -> None:
    from mcp.types import ClientRequest

    params: dict[str, Any]
    if method == "prompts/get":
        params = {"name": "example", "arguments": {key: 1}}
    else:
        params = {
            "ref": {"type": "ref/prompt", "name": "example"},
            "argument": {"name": "example", "value": ""},
            "context": {"arguments": {key: 1}},
        }
    with pytest.raises(ValidationError) as caught:
        ClientRequest.model_validate({"method": method, "params": params})
    error = caught.value
    assert any(key in detail["loc"] for detail in error.errors())
    message = f"Failed to validate request: {error}"
    record = logging.LogRecord(
        "root",
        logging.WARNING,
        sdk_session.BaseSession._receive_loop.__code__.co_filename,
        1,
        message,
        (),
        None,
    )

    assert _PROTOCOL_SESSION_FILTER.filter(record)

    rendered = logging.Formatter().format(record)
    assert rendered == message.splitlines()[0]
    assert "for ClientRequest" in rendered
    assert "private-argument-key" not in rendered
    assert "forged entry" not in rendered
    assert "input_value=" not in rendered
    assert len(rendered.splitlines()) == 1
    assert record.args == ()
    assert record.exc_info is None


@pytest.mark.parametrize("cached", [False, True], ids=["exc-info", "exc-text"])
def test_wrapped_validation_error_omits_input_from_exception_text(cached: bool) -> None:
    try:
        try:
            _parse_private_message()
        except ValidationError as exc:
            raise RuntimeError(str(exc)) from None
    except RuntimeError:
        record = logging.LogRecord(
            "mcp.server.lowlevel.server",
            logging.ERROR,
            __file__,
            1,
            "Failed to read stream",
            (),
            sys.exc_info(),
        )
    formatter = logging.Formatter()
    assert _SECRET in formatter.format(record)
    if cached:
        assert record.exc_text is not None
        record.exc_info = None

    assert _PROTOCOL_PAYLOAD_FILTER.filter(record)

    rendered = formatter.format(record)
    assert "Failed to read stream" in rendered
    assert "RuntimeError" in rendered
    assert "Traceback (most recent call last)" in rendered
    assert "Input should be a valid string" in rendered
    assert "input_value=" not in rendered
    assert _SECRET not in rendered
    assert record.exc_info is None


def test_personal_token_mask_uses_identity_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis import mcp_identity

    monkeypatch.setattr(mcp_identity, "TOKEN_PREFIX", "test.identity+")
    credential = mcp_identity.TOKEN_PREFIX + "a" * 43
    near_match = "testXidentity" + "a" * 43

    rendered = server._mask_protocol_secrets(f"failed: {credential}; {near_match}")

    assert credential not in rendered
    assert rendered == f"failed: [redacted]; {near_match}"


def test_filter_fallback_preserves_masked_static_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.mcp_identity import TOKEN_PREFIX

    def fail_render(exc: BaseException) -> traceback.TracebackException:
        raise RuntimeError(_SECRET)

    credential = TOKEN_PREFIX + "a" * 43
    record = _parser_record()
    record.msg = f"Failed to parse message with bearer {credential}"
    assert _SECRET in logging.Formatter().format(record)
    monkeypatch.setattr(server, "_protocol_exception_trace", fail_render)

    assert _PROTOCOL_PAYLOAD_FILTER.filter(record)

    rendered = logging.Formatter().format(record)
    assert "Failed to parse message with bearer [redacted]" in rendered
    assert "ValidationError" in rendered
    assert "(details unavailable)" in rendered
    assert _SECRET not in rendered
    assert credential not in rendered
    assert record.args == ()
    assert record.exc_info is None
    assert record.exc_text is None
    assert record.stack_info is None

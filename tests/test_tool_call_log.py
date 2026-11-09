"""Tests for the per-MCP-call audit log (``_log_tool_call``).

The audit line is the diagnostic surface for "what did the agent
actually ask for?" — see :func:`precis.tools.core._log_tool_call`.
These tests pin the two contracts that make it useful for debugging:

* the search query (``q=``) and the addressing / citation fields the
  agent fumbles most are sampled verbatim (truncated), not dropped;
* a **failed** call logs at WARNING with a fuller payload (so it
  survives a server deployed at ``log_level=WARNING`` and the exact
  misuse is reconstructable).
"""

from __future__ import annotations

import logging

from precis.tools import core
from precis.tools.core import _log_tool_call


def test_success_call_samples_query_and_scalars(caplog) -> None:
    """A successful call logs at INFO and includes ``q=`` + scalars."""
    with caplog.at_level(logging.INFO, logger="precis.tools.mcp_calls"):
        _log_tool_call(
            verb="search",
            payload={"kind": "paper", "q": "photocatalytic NOx reduction"},
            duration_ms=12.0,
            error=False,
        )
    rec = caplog.records[-1]
    assert rec.levelno == logging.INFO
    msg = rec.getMessage()
    assert "verb=search" in msg
    # The query — the single most important "what is the agent asking"
    # field — must be present verbatim, not reduced to its presence.
    assert "photocatalytic NOx reduction" in msg


def test_failed_call_logs_at_warning_with_full_payload(caplog) -> None:
    """A failed call escalates to WARNING and widens the capture.

    The citation/finding fields agents fumble (``cited_in``, ``title``)
    and any otherwise-unsampled kwarg must appear so the exact misuse
    is reconstructable from the log alone.
    """
    with caplog.at_level(logging.WARNING, logger="precis.tools.mcp_calls"):
        _log_tool_call(
            verb="put",
            payload={
                "kind": "finding",
                "title": "gate-bias 2.4 kV / 30 s",
                "cited_in": "doi:10.1234/xyz",
                "scope": {"electrode": "Cu"},
            },
            duration_ms=3.0,
            error=True,
        )
    rec = caplog.records[-1]
    assert rec.levelno == logging.WARNING
    msg = rec.getMessage()
    assert "error=True" in msg
    assert "doi:10.1234/xyz" in msg  # the rejected cited_in
    assert "gate-bias" in msg  # title — only captured on the error path


def test_long_query_is_truncated(caplog) -> None:
    """Oversize free-text is bounded so the line stays grep-friendly."""
    with caplog.at_level(logging.INFO, logger="precis.tools.mcp_calls"):
        _log_tool_call(
            verb="search",
            payload={"q": "x" * 500},
            duration_ms=1.0,
            error=False,
        )
    msg = caplog.records[-1].getMessage()
    assert "…" in msg
    assert "x" * 500 not in msg


# ── response size (memory-ablation slice 1) ─────────────────────────────


def test_log_line_carries_response_bytes_and_token_estimate(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="precis.tools.mcp_calls"):
        _log_tool_call(
            verb="get",
            payload={"kind": "paper"},
            duration_ms=1.0,
            error=False,
            response_bytes=1003,
        )
    msg = caplog.records[-1].getMessage()
    assert "response_bytes=1003" in msg
    assert "response_tokens_est=250" in msg  # 1003 // 4


def test_log_line_marks_unmeasured_response(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="precis.tools.mcp_calls"):
        _log_tool_call(verb="get", payload={}, duration_ms=1.0, error=False)
    msg = caplog.records[-1].getMessage()
    assert "response_bytes=-" in msg
    assert "response_tokens_est=-" in msg


def test_result_text_bytes_counts_utf8_and_sums_text_parts() -> None:
    from mcp.types import CallToolResult, ImageContent, TextContent

    assert core._result_text_bytes("héllo") == 6  # é is two bytes
    multi = CallToolResult(
        content=[
            TextContent(type="text", text="abcd"),
            ImageContent(type="image", data="AAAA", mimeType="image/png"),
            TextContent(type="text", text="éf"),
        ]
    )
    assert core._result_text_bytes(multi) == 4 + 3  # image adds nothing
    assert core._result_text_bytes(CallToolResult(content=[])) == 0


class _FakeRuntime:
    store = None

    def __init__(self, body: str = "ok", is_error: bool = False) -> None:
        self.body = body
        self.is_error = is_error
        self.raises: Exception | None = None

    def dispatch_with_status(self, verb, payload, *, fold=False):
        if self.raises is not None:
            raise self.raises
        return self.body, self.is_error

    def render_error(self, err: Exception) -> str:
        return f"[error:Boom] {err}"


def _dispatch_logged(monkeypatch, caplog, rt):
    monkeypatch.setattr(core, "_get_runtime", lambda: rt)
    with caplog.at_level(logging.INFO, logger="precis.tools.mcp_calls"):
        result = core._dispatch_as_user("get", {"kind": "skill"})
    return result, caplog.records[-1]


def test_dispatch_logs_response_bytes_on_success(monkeypatch, caplog) -> None:
    result, rec = _dispatch_logged(monkeypatch, caplog, _FakeRuntime("é" * 50))
    assert result == "é" * 50
    assert rec.levelno == logging.INFO
    assert "response_bytes=100 " in rec.getMessage()
    assert "response_tokens_est=25 " in rec.getMessage()


def test_dispatch_logs_response_bytes_on_error_result(monkeypatch, caplog) -> None:
    _, rec = _dispatch_logged(
        monkeypatch, caplog, _FakeRuntime("[error:BadInput] nope", is_error=True)
    )
    assert rec.levelno == logging.WARNING
    msg = rec.getMessage()
    assert "response_bytes=21 " in msg  # len("[error:BadInput] nope")
    assert "response_tokens_est=5 " in msg


def test_dispatch_logs_response_bytes_on_exception(monkeypatch, caplog) -> None:
    rt = _FakeRuntime()
    rt.raises = RuntimeError("kaput")
    _, rec = _dispatch_logged(monkeypatch, caplog, rt)
    assert rec.levelno == logging.WARNING
    assert f"response_bytes={len('[error:Boom] kaput')} " in rec.getMessage()


def test_dispatch_sums_text_parts_of_multipart_result(monkeypatch, caplog) -> None:
    from precis.runtime.dispatch import _RESPONSE_IMAGES

    class _Img:
        data = b"\x89PNG"
        mime = "image/png"

    class _ImgRuntime(_FakeRuntime):
        def dispatch_with_status(self, verb, payload, *, fold=False):
            sink = _RESPONSE_IMAGES.get()
            assert sink is not None
            sink.append(_Img())
            return "caption", False

    result, rec = _dispatch_logged(monkeypatch, caplog, _ImgRuntime())
    assert len(result.content) == 2
    # Text part only; the base64 image block is not counted.
    assert "response_bytes=7 " in rec.getMessage()
    assert "response_tokens_est=1 " in rec.getMessage()


def test_ledger_row_and_log_line_agree_on_bytes(
    runtime_with_store, monkeypatch, caplog
) -> None:
    """The ``tool_calls.result_bytes`` row and the log line's
    ``response_bytes`` are the same count for one call."""
    monkeypatch.setattr(core, "_get_runtime", lambda: runtime_with_store)
    with caplog.at_level(logging.INFO, logger="precis.tools.mcp_calls"):
        result = core._dispatch_as_user(
            "get", {"kind": "skill", "id": "precis-status-help"}
        )
    assert isinstance(result, str)
    expected = len(result.encode("utf-8"))
    assert f"response_bytes={expected} " in caplog.records[-1].getMessage()

    store = runtime_with_store.store
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT result_bytes FROM tool_calls WHERE verb='get' "
            "AND kind='skill' AND ts > now() - interval '1 minute' "
            "ORDER BY call_id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert row[0] == expected

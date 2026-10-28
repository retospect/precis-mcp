"""Canonical ``search(page_size=...)`` boundary.

The full Python function no longer accepts the removed ``k``/``limit``
spellings. Real-MCP refusal coverage for those names lives in
``test_mcp_slim_schema.py``; these tests pin the canonical forwarding and
validation that remains on the full function.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.types import CallToolResult

from precis.tools import core as tools_core


@pytest.fixture
def captured_payload(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def _fake_dispatch(verb: str, payload: dict[str, Any], ctx: object = None) -> str:
        captured["verb"] = verb
        captured.update(payload)
        return "ok"

    monkeypatch.setattr(tools_core, "_dispatch", _fake_dispatch)
    return captured


def _body(out: Any) -> str:
    if isinstance(out, CallToolResult):
        return out.content[0].text  # type: ignore[union-attr]
    return out


def _is_error(out: Any) -> bool:
    return isinstance(out, CallToolResult) and bool(out.isError)


def test_page_size_forwards(captured_payload: dict[str, Any]) -> None:
    out = tools_core.search(kind="memory", q="x", page_size=7)
    assert not _is_error(out)
    assert captured_payload["page_size"] == 7


def test_no_size_arg_forwards_none(captured_payload: dict[str, Any]) -> None:
    out = tools_core.search(kind="memory", q="x")
    assert not _is_error(out)
    assert captured_payload["page_size"] is None


def test_removed_size_params_are_not_on_signature() -> None:
    import inspect

    params = inspect.signature(tools_core.search).parameters
    assert "k" not in params
    assert "limit" not in params


def test_page_size_must_be_positive() -> None:
    out = tools_core.search(kind="memory", q="x", page_size=0)
    assert _is_error(out)
    assert "page_size must be a positive integer" in _body(out)


def test_page_size_cap() -> None:
    out = tools_core.search(kind="memory", q="x", page_size=101)
    assert _is_error(out)
    assert "exceeds maximum 100" in _body(out)


def test_unique_per_forwards(captured_payload: dict[str, Any]) -> None:
    out = tools_core.search(kind="paper", q="x", unique_per="chunk")
    assert not _is_error(out)
    assert captured_payload["unique_per"] == "chunk"


def test_unique_per_unknown_value_is_bad_input() -> None:
    out = tools_core.search(kind="paper", q="x", unique_per="chapter")
    assert _is_error(out)
    assert "unique_per" in _body(out)

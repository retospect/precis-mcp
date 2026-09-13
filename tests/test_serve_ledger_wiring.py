"""Tests for the ``ctx``-to-serve-ledger wiring at the tool boundary
(docs/backlog/skill-graph.md slice 1).

``tools.core.get``/``search`` bind the session carried by FastMCP's
injected ``Context`` for the duration of the dispatch call, so
:mod:`precis.handlers.skill` can key the serve ledger off the real MCP
session without threading an extra kwarg through the generic handler
dispatch. These tests stub ``_dispatch`` so they don't need a live
runtime/store — only the binding contract is under test here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from precis import serve_ledger
from precis.tools import core

if TYPE_CHECKING:
    from mcp.server.fastmcp import Context


class _FakeSession:
    pass


def _fake_ctx(session: object | None) -> Context:
    """A duck-typed stand-in for FastMCP's Context — only ``.session``
    is read by the wiring under test; cast keeps mypy on the real
    signature at the call sites."""

    class _FakeCtx:
        def __init__(self) -> None:
            self.session = session

    return cast("Context", _FakeCtx())


def test_get_binds_ctx_session_around_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object | None] = {}

    def fake_dispatch(verb: str, payload: dict) -> str:
        captured["verb"] = verb
        captured["session"] = serve_ledger._current_session.get()
        return "ok"

    monkeypatch.setattr(core, "_dispatch", fake_dispatch)
    session = _FakeSession()

    result = core.get(kind="skill", id="x", ctx=_fake_ctx(session))

    assert result == "ok"
    assert captured["verb"] == "get"
    assert captured["session"] is session
    # Unbound again once the call returns.
    assert serve_ledger._current_session.get() is None


def test_search_binds_ctx_session_around_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object | None] = {}

    def fake_dispatch(verb: str, payload: dict) -> str:
        captured["session"] = serve_ledger._current_session.get()
        return "ok"

    monkeypatch.setattr(core, "_dispatch", fake_dispatch)
    session = _FakeSession()

    result = core.search(kind="skill", q="orientation", ctx=_fake_ctx(session))

    assert result == "ok"
    assert captured["session"] is session
    assert serve_ledger._current_session.get() is None


def test_get_without_ctx_binds_no_session(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object | None] = {}

    def fake_dispatch(verb: str, payload: dict) -> str:
        captured["session"] = serve_ledger._current_session.get()
        return "ok"

    monkeypatch.setattr(core, "_dispatch", fake_dispatch)
    core.get(kind="skill", id="x")
    assert captured["session"] is None


def test_get_search_ctx_excluded_from_wire_schema() -> None:
    """``ctx`` is FastMCP's injected per-request Context — it must not
    leak into the advertised ``inputSchema`` for either tool."""
    from precis import server

    for name in ("get", "search"):
        tool = server.mcp._tool_manager.get_tool(name)
        assert tool is not None, f"{name!r} tool missing from FastMCP manager"
        assert "ctx" not in tool.parameters.get("properties", {}), (
            f"{name}'s wire schema leaked the injected ctx= parameter"
        )


def test_ctx_excluded_from_cli_parameters() -> None:
    """The CLI argparse adapter must not advertise a ``--ctx`` flag —
    it isn't CLI-constructible and no CLI caller has an MCP session."""
    from precis.tools import get_tool_info

    for name in ("get", "search"):
        info = get_tool_info(name)
        assert "ctx" not in info["parameters"]


def test_skill_search_rejects_plural_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Single-kind skill search rejects the cross-kind ``tags=`` filter
    (SkillHandler would silently swallow it) and redirects to the
    singular ``tag=`` topic axis, pre-dispatch."""

    class _FakeRuntime:
        def render_error(self, err: Exception) -> str:
            return f"rendered: {err}"

    def fail_dispatch(verb: str, payload: dict) -> str:
        raise AssertionError("tags= on kind='skill' must not reach dispatch")

    monkeypatch.setattr(core, "_dispatch", fail_dispatch)
    monkeypatch.setattr(core, "_get_runtime", lambda: _FakeRuntime())

    result = cast(Any, core.search(kind="skill", q="x", tags=["orientation"]))
    assert getattr(result, "isError", False)
    assert "tag=" in result.content[0].text

    # Other kinds keep the plural filter untouched.
    monkeypatch.setattr(core, "_dispatch", lambda verb, payload: "ok")
    assert core.search(kind="paper", q="x", tags=["STATUS:done"]) == "ok"

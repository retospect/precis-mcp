"""Tests for the MCP boundary `args=` kwarg on the `get` tool.

The `get` tool exposes a generic `args: dict | None` kwarg that lets
view-specific parameters (e.g. python's callgraph `entry`/`depth`,
runtrace `argv`/`timeout`) cross the MCP boundary without bloating
the four-tool surface.

These tests exercise `precis.server.get` end-to-end with a runtime
backing it. They mount the runtime onto the module-level `_runtime`
slot for the duration of each test, mirroring how the FastMCP loop
would set it up via `_init_runtime`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult

from precis import server
from precis.handlers.python import PythonHandler
from precis.python_index import RepoCache
from precis.runtime import PrecisRuntime


def _body(out: Any) -> str:
    """Pull the text body out of a tool result.

    The MCP boundary returns ``str`` on success and a ``CallToolResult``
    with ``isError=True`` on failure (so the protocol-level flag is
    correct — see MCP critic MAJOR on errors-as-strings). Tests that
    just want to grep the textual body don't care which shape they
    got; this helper papers over the difference.
    """
    if isinstance(out, CallToolResult):
        # Successful tools return a single TextContent block; errors
        # carry the same shape with isError=True.
        return out.content[0].text  # type: ignore[union-attr]
    return out


def _is_error(out: Any) -> bool:
    """True when the tool result carries the protocol-level error flag."""
    return isinstance(out, CallToolResult) and bool(out.isError)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def server_runtime(tmp_path: Path, runtime: PrecisRuntime) -> Iterator[PrecisRuntime]:
    """Mount a `runtime` instance into `precis.server._runtime` and add
    a python handler pointing at a fixture repo, so `server.get(...)`
    can be invoked directly the way FastMCP would call it.

    The fixture repo carries a single module so callgraph + outline
    paths have something to render.
    """
    # Build a tiny indexable repo.
    pkg = tmp_path / "demo" / "demopkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "m.py").write_text(
        "def helper():\n    return 1\n\n\ndef main():\n    return helper()\n"
    )

    # Splice a python handler into the runtime's registry by
    # invoking ``_register_with`` directly. Under the new dispatch
    # layer the registry is a flat (kind, verb, mode) -> callable
    # table plus a handlers-by-kind map; ``_register_with`` mutates
    # both in one step, atomic for the caller.
    py = PythonHandler(roots={"demo": tmp_path / "demo"}, cache=RepoCache())
    py._register_with(runtime.hub)

    server._runtime = runtime
    try:
        yield runtime
    finally:
        server._runtime = None


# ---------------------------------------------------------------------------
# Reserved-key validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "shadowed",
    [{"id": "demo"}, {"view": "outline"}, {"q": "x"}, {"kind": "python"}],
)
def test_args_rejects_reserved_keys(
    server_runtime: PrecisRuntime, shadowed: dict
) -> None:
    """Passing kind/id/view/q inside args= is a programming mistake;
    surface it at the MCP boundary so the recovery hint is sharp."""
    out = server.get(kind="python", id="demo", args=shadowed)
    assert _is_error(out)
    body = _body(out)
    assert "[error:BadInput]" in body
    assert "shadows the explicit kwargs" in body


def test_args_reserved_check_lists_all_overlaps(
    server_runtime: PrecisRuntime,
) -> None:
    """Multiple overlapping keys all surface in the message."""
    out = server.get(kind="python", id="demo", args={"id": "x", "view": "outline"})
    assert _is_error(out)
    body = _body(out)
    assert "'id'" in body and "'view'" in body


# ---------------------------------------------------------------------------
# Pass-through to handler
# ---------------------------------------------------------------------------


def test_args_passes_through_to_python_callgraph(
    server_runtime: PrecisRuntime,
) -> None:
    """The whole point: `entry=` and `depth=` reach the python handler
    via args= and produce a real callgraph response."""
    out = server.get(
        kind="python",
        id="demo",
        view="callgraph",
        args={"entry": "demopkg.m.main", "depth": 2},
    )
    assert "Static call graph from demo::demopkg.m.main" in out
    assert "depth=2" in out
    assert "demopkg.m.helper" in out


def test_args_pass_through_supports_cross_repo_flag(
    server_runtime: PrecisRuntime,
) -> None:
    """`cross_repo=True` is a bool that must survive JSON-ish transport."""
    out = server.get(
        kind="python",
        id="demo",
        view="callgraph",
        args={"entry": "demopkg.m.main", "depth": 1, "cross_repo": True},
    )
    assert "cross-repo" in out


def test_args_none_is_noop(server_runtime: PrecisRuntime) -> None:
    """`args=None` (the default) leaves the dispatch payload identical
    to the no-args code path."""
    with_none = server.get(kind="python", id="demo", view="toc")
    explicit_none = server.get(kind="python", id="demo", view="toc", args=None)
    assert with_none == explicit_none
    assert "demopkg" in with_none


def test_args_empty_dict_is_noop(server_runtime: PrecisRuntime) -> None:
    """An empty dict shouldn't cause any reserved-key error or change
    behaviour. Treat it the same as no args."""
    out = server.get(kind="python", id="demo", view="toc", args={})
    assert "[error:" not in out
    assert "demopkg" in out


# ---------------------------------------------------------------------------
# Bad inputs from the handler still surface
# ---------------------------------------------------------------------------


def test_args_with_bad_value_surfaces_handler_error(
    server_runtime: PrecisRuntime,
) -> None:
    """If the handler rejects an arg (e.g. callgraph depth out of range),
    the error round-trips back through the runtime's error renderer
    rather than crashing."""
    out = server.get(
        kind="python",
        id="demo",
        view="callgraph",
        args={"entry": "demopkg.m.main", "depth": 999},
    )
    assert _is_error(out)
    body = _body(out)
    assert "[error:BadInput]" in body
    assert "depth must be" in body


def test_args_callgraph_unknown_entry_returns_not_found(
    server_runtime: PrecisRuntime,
) -> None:
    out = server.get(
        kind="python",
        id="demo",
        view="callgraph",
        args={"entry": "demopkg.m.does_not_exist"},
    )
    assert _is_error(out)
    body = _body(out)
    assert "[error:NotFound]" in body
    assert "callgraph entry" in body


# ---------------------------------------------------------------------------
# Backwards compatibility — existing tools still work without args=
# ---------------------------------------------------------------------------


def test_get_without_args_still_works(server_runtime: PrecisRuntime) -> None:
    out = server.get(kind="python", id="demo", view="outline")
    assert "demopkg" in out
    assert "[error:" not in out


def test_calc_through_get_tool_unchanged(server_runtime: PrecisRuntime) -> None:
    out = server.get(kind="calc", id="2+3*4")
    assert "14" in out

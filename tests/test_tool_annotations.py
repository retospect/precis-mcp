"""MCP ``ToolAnnotations`` on the registered verb tools (DB-free).

Spec: ``docs/conventions/mcp-authoring.md`` § Tool annotations. Hints are
set only where true for every kind behind the verb; ``put``/``edit``/
``tag``/``link`` are mixed per kind and carry none.
"""

from __future__ import annotations

import pytest


def _tool(name: str):
    from precis import server

    tool = server.mcp._tool_manager.get_tool(name)
    assert tool is not None, f"verb {name!r} not registered"
    return tool


@pytest.mark.parametrize("verb", ["get", "search", "more"])
def test_read_only_verbs(verb: str) -> None:
    ann = _tool(verb).annotations
    assert ann is not None
    assert ann.readOnlyHint is True
    assert ann.destructiveHint is None


def test_delete_is_destructive() -> None:
    ann = _tool("delete").annotations
    assert ann is not None
    assert ann.destructiveHint is True
    assert ann.readOnlyHint is None


@pytest.mark.parametrize("verb", ["put", "edit", "tag", "link"])
def test_mixed_verbs_carry_no_annotations(verb: str) -> None:
    assert _tool(verb).annotations is None


@pytest.mark.parametrize(
    "verb", ["get", "search", "put", "edit", "delete", "tag", "link"]
)
def test_no_idempotent_or_open_world_hints(verb: str) -> None:
    ann = _tool(verb).annotations
    if ann is not None:
        assert ann.idempotentHint is None
        assert ann.openWorldHint is None

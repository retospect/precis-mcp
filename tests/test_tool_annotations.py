"""MCP tool annotations for the typed tools/list response (gr772874)."""

from __future__ import annotations

import asyncio

from mcp.types import ListToolsResult


def test_tools_list_annotations() -> None:
    from precis import server

    result = ListToolsResult(tools=asyncio.run(server.mcp.list_tools()))
    wire = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    read_only = {"readOnlyHint": True}
    additive = {"readOnlyHint": False, "destructiveHint": False}
    destructive = {"readOnlyHint": False, "destructiveHint": True}
    assert {tool["name"]: tool["annotations"] for tool in wire["tools"]} == {
        "get": read_only,
        "search": read_only,
        "put": additive,
        "edit": additive,
        "delete": destructive,
        "tag": additive,
        "link": additive,
    }

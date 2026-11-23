"""MCP annotations and metadata on the typed tools/list response."""

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


def test_tools_list_requests_eager_loading_for_every_verb() -> None:
    from precis import server

    result = ListToolsResult(tools=asyncio.run(server.mcp.list_tools()))
    wire = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert {
        tool["name"]: tool["_meta"]["anthropic/alwaysLoad"] for tool in wire["tools"]
    } == {
        "get": True,
        "search": True,
        "put": True,
        "edit": True,
        "delete": True,
        "tag": True,
        "link": True,
    }

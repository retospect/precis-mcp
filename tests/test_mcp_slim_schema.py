"""Slim advertised MCP schema for put/edit/search (docs/backlog/mcp-verb-schema-diet.md).

Pins, over the real FastMCP ``call_tool`` path (not the Python functions):

- the advertised schema stays small (ratchet);
- a moved kwarg passed at top level still works and carries a deprecation
  note (FastMCP's default arg model would silently drop it);
- the same kwarg inside ``args`` works with no note;
- an unknown key is a ``BadInput`` listing the kind's accepted keys with
  types/defaults and a ``next:`` skill pointer.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest
from mcp.types import CallToolResult

from precis import server
from precis.tools import core as tools_core
from precis.tools.mcp_slim import CORE_PARAMS

#: Total advertised chars (inputSchema JSON + description) across all eight
#: verbs. Measured 2026-10-08 after the diet (see test output on failure);
#: the ceiling is ~5% above that. Before the diet: 30.7k (13.1k after).
_SCHEMA_CHAR_CEILING = 13_800


def _advertised_chars(name: str) -> int:
    tool = server.mcp._tool_manager.get_tool(name)
    assert tool is not None
    return len(json.dumps(tool.parameters)) + len(tool.description or "")


def _text(result: Any) -> str:
    if isinstance(result, CallToolResult):
        return "".join(getattr(b, "text", "") for b in result.content)
    if isinstance(result, tuple):  # (content, structured)
        result = result[0]
    if isinstance(result, list):
        return "".join(getattr(b, "text", "") for b in result)
    return str(result)


@pytest.fixture
def mcp_runtime(runtime_with_store: Any) -> Iterator[None]:
    saved = (server._runtime, tools_core._runtime)
    server._runtime = runtime_with_store
    tools_core._runtime = runtime_with_store
    try:
        yield
    finally:
        server._runtime, tools_core._runtime = saved


def _call(name: str, arguments: dict[str, Any]) -> str:
    return _text(asyncio.run(server.mcp.call_tool(name, arguments)))


def test_schema_size_ratchet() -> None:
    names = ["get", "search", "put", "edit", "delete", "tag", "link", "more"]
    sizes = {n: _advertised_chars(n) for n in names}
    total = sum(sizes.values())
    assert total <= _SCHEMA_CHAR_CEILING, f"{total} > {_SCHEMA_CHAR_CEILING}: {sizes}"


@pytest.mark.parametrize("verb", sorted(CORE_PARAMS))
def test_advertised_params_are_exactly_the_core_set(verb: str) -> None:
    tool = server.mcp._tool_manager.get_tool(verb)
    assert tool is not None
    advertised = set(tool.parameters["properties"])
    assert advertised == set(CORE_PARAMS[verb])
    # the full in-process signature is untouched
    full = getattr(tools_core, verb)
    import inspect

    assert len(inspect.signature(full).parameters) > len(advertised)


def test_top_level_moved_kwarg_works_with_deprecation_note(mcp_runtime: None) -> None:
    # ``exclude`` is a full-search kwarg that is no longer advertised.
    out = _call("search", {"q": "anything", "kind": "paper", "exclude": ["x"]})
    assert "[error" not in out, out
    assert (
        "note: pass exclude inside args={...}; top-level exclude is deprecated" in out
    )


def test_moved_kwarg_inside_args_has_no_note(mcp_runtime: None) -> None:
    out = _call(
        "search", {"q": "anything", "kind": "paper", "args": {"exclude": ["x"]}}
    )
    assert "[error" not in out, out
    assert "deprecated" not in out


def test_unknown_top_level_key_is_not_silently_dropped(mcp_runtime: None) -> None:
    out = _call("put", {"kind": "gripe", "text": "x", "zzz_bogus": 1})
    assert "[error:BadInput]" in out, out
    assert "zzz_bogus" in out


def test_unknown_args_key_lists_typed_accepted_keys(mcp_runtime: None) -> None:
    out = _call("put", {"kind": "gripe", "text": "x", "args": {"zzz_bogus": 1}})
    assert "[error:BadInput]" in out, out
    assert "zzz_bogus" in out
    # typed accepted list: ``name: annotation = default``
    assert "text: " in out and " = None" in out, out
    assert "get(kind='skill', id='precis-gripe-help')" in out


def test_legacy_top_level_json_string_list_is_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FastMCP's pre_parse_json only covers declared fields; a legacy
    top-level ``llm_models`` JSON string must still reach the handler as a list."""
    seen: dict[str, Any] = {}

    def fake_dispatch(verb: str, payload: dict[str, Any]) -> str:
        seen.update(payload)
        return "ok"

    monkeypatch.setattr(tools_core, "_dispatch", fake_dispatch)
    out = _call("put", {"kind": "job", "llm_models": '["a","b"]'})
    assert seen["llm_models"] == ["a", "b"], seen
    assert "top-level llm_models is deprecated" in out
    # inside args: same coercion, no note
    seen.clear()
    out = _call("put", {"kind": "job", "args": {"llm_models": '["c"]'}})
    assert seen["llm_models"] == ["c"], seen
    assert "deprecated" not in out
    # a str-typed target is left alone
    seen.clear()
    _call("put", {"kind": "job", "args": {"idem_key": '["c"]'}})
    assert seen["idem_key"] == '["c"]'


def test_coerce_extras_uses_handler_annotation() -> None:
    from precis.runtime.dispatch import _coerce_extras, coerce_json_container

    class H:
        def put(self, *, items: list[str] | None = None, name: str | None = None):
            pass

    out = _coerce_extras(H, "put", {"items": '["x"]', "name": '["x"]'})
    assert out == {"items": ["x"], "name": '["x"]'}
    assert coerce_json_container("not json", "list[str]") == "not json"
    assert coerce_json_container('{"a": 1}', "list[str]") == '{"a": 1}'

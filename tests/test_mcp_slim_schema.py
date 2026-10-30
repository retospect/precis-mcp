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
from precis.store import Store
from precis.tools import core as tools_core
from precis.tools.mcp_slim import CORE_PARAMS
from tests._refs import insert_gripe

#: Total advertised chars (inputSchema JSON + description) across all seven
#: verbs. Measured 2026-10-08 after the diet (see test output on failure);
#: the ceiling is ~5% above that. Before the diet: 30.7k (13.1k after).
#: Raised 13.8k -> 14.0k for delete's args= (hide a gripe/todo comment) and the
#: put-verb comment clause (measured 13971 on 2026-10-11).
_SCHEMA_CHAR_CEILING = 14_000


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
    names = ["get", "search", "put", "edit", "delete", "tag", "link"]
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


def test_legacy_paging_alias_runs_with_interpreted_line(
    mcp_runtime: None, store: Store
) -> None:
    """gr530420: ``limit``/``k``/``offset`` are verb-level aliases the
    full search function consumes — the slim layer must promote them,
    never route them into the kind's ``args=`` (where they die on the
    unknown-key gate)."""
    for i in range(5):
        insert_gripe(store, f"zzq alias probe {i}")
        store.insert_ref(kind="todo", slug=None, title=f"zzq alias probe {i}")

    for kind in ("gripe", "todo"):
        out = _call("search", {"kind": kind, "limit": 3})
        assert "[error" not in out, out
        assert out.startswith("interpreted: 'limit' read as 'page_size'"), out
        hits = [ln for ln in out.splitlines() if "zzq alias probe" in ln]
        assert len(hits) == 3, out

    # offset=0 is an accepted no-op under the bounded top-N model.
    out = _call("search", {"kind": "gripe", "offset": 0, "page_size": 3})
    assert "[error" not in out, out
    assert out.startswith("interpreted: 'offset=0' is a no-op"), out
    hits = [ln for ln in out.splitlines() if "zzq alias probe" in ln]
    assert len(hits) == 3, out


def test_positive_offset_refuses_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """offset>0 asks for results past the bounded top-N set — the same
    refusal shape as page=2 (gr530420)."""

    def unexpected(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("offset>0 reached dispatch")

    monkeypatch.setattr(tools_core, "_dispatch", unexpected)
    out = _call("search", {"kind": "gripe", "offset": 3})
    assert "[error:BadInput]" in out, out
    assert "no longer paginates" in out
    assert "page_size" in out


def test_args_nested_paging_alias_refuses_naming_page_size(
    mcp_runtime: None,
) -> None:
    """``args={'limit': 3}`` names no kind arg — the refusal's ``next:``
    names the canonical top-level spelling (gr530420)."""
    out = _call("search", {"kind": "gripe", "args": {"limit": 3}})
    assert "[error:BadInput]" in out, out
    next_line = next(
        (ln for ln in out.splitlines() if ln.strip().startswith("next:")), ""
    )
    assert "page_size" in next_line, out
    assert "args=" not in next_line.split("page_size")[0], out


def test_unknown_top_level_key_is_not_silently_dropped(mcp_runtime: None) -> None:
    out = _call("put", {"kind": "gripe", "text": "x", "zzz_bogus": 1})
    assert "[error:BadInput]" in out, out
    assert "zzz_bogus" in out


def test_unknown_args_key_lists_typed_accepted_keys(mcp_runtime: None) -> None:
    out = _call("put", {"kind": "gripe", "text": "x", "args": {"zzz_bogus": 1}})
    assert "[error:BadInput]" in out, out
    assert "zzz_bogus" in out
    # typed accepted list of args= keys only: ``name: annotation = default``;
    # core params (text=) are top-level, so they are not listed.
    assert "prio: " in out and " = None" in out, out
    assert "text: " not in out, out
    # the accepted list is printed once (options:), not again under next:
    assert out.count("prio: ") == 1, out


@pytest.mark.parametrize("verb", ["get", "put", "edit", "search"])
def test_description_says_unknown_args_key_fails(verb: str) -> None:
    """gr524246: the accepted-keys listing is the FAILURE path — a call
    with an unknown key errors with BadInput. The advertised description
    must say the call fails, or an agent can't tell discovery from success."""
    tool = server.mcp._tool_manager.get_tool(verb)
    assert tool is not None
    # descriptions wrap mid-sentence — collapse whitespace before matching
    desc = " ".join((tool.description or "").split())
    assert "unknown key fails the call" in desc, desc
    assert "BadInput" in desc, desc


def test_title_on_a_text_kind_points_at_text(mcp_runtime: None) -> None:
    """``title=`` is core for put, but todo takes its title in ``text=``
    (Fable dogfood 2026-10-08)."""
    out = _call("put", {"kind": "todo", "title": "x"})
    assert "[error:BadInput]" in out, out
    assert "todo: the title is the first line of text=" in out, out
    assert "text: " not in out, out


def test_legacy_top_level_json_string_list_is_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FastMCP's pre_parse_json only covers declared fields; a legacy
    top-level ``llm_models`` JSON string must still reach the handler as a list."""
    seen: dict[str, Any] = {}

    def fake_dispatch(verb: str, payload: dict[str, Any], ctx: object = None) -> str:
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


def test_coerce_parses_stringified_scalars_by_annotation() -> None:
    """A client has no schema type for an undeclared top-level key, so
    ``prio=9`` arrives as ``'9'`` (dogfood 2026-10-08)."""
    from precis.runtime.dispatch import coerce_json_container

    assert coerce_json_container("9", "int | None") == 9
    assert coerce_json_container("2", "float") == 2
    assert coerce_json_container("true", "bool | None") is True
    assert coerce_json_container("9", "str | None") == "9"
    assert coerce_json_container("9", "int | str") == "9"
    assert coerce_json_container("x", "int") == "x"


def test_stringified_top_level_scalar_reaches_the_verb_as_int(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import precis.tools.core as tools_core

    seen: dict[str, Any] = {}

    def fake_dispatch(verb: str, payload: dict[str, Any], ctx: object = None) -> str:
        seen.update(payload)
        return "ok"

    monkeypatch.setattr(tools_core, "_dispatch", fake_dispatch)
    out = _call("search", {"kind": "paper", "q": "x", "per_paper": "2"})
    assert seen["per_paper"] == 2
    assert "per_paper must be" not in out, out
    assert "note: pass per_paper inside args=" in out


def _recording_slim(seen: list[dict[str, Any]]) -> Any:
    import functools

    from precis.tools.mcp_slim import make_slim_verb

    @functools.wraps(tools_core.put)
    def recorder(**kw: Any) -> str:
        seen.append(kw)
        return "ok"

    return make_slim_verb("put", recorder)


def test_pcb_import_epro_source_in_args_reaches_handler(
    mcp_runtime: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr496397: ``source`` is a core.put name but pcb takes it in args=."""
    from precis.handlers.pcb import PcbHandler

    got: dict[str, Any] = {}

    def fake(self: Any, slug: str, args: dict[str, Any]) -> str:
        got.update(args)
        return "imported"

    monkeypatch.setattr(PcbHandler, "_op_import_epro", fake)
    out = _call(
        "put",
        {
            "kind": "pcb",
            "id": "copy-c",
            "args": {"op": "import_epro", "source": "export:board-b"},
        },
    )
    assert "does not accept" not in out, out
    assert got.get("source") == "export:board-b"


def test_pcb_pins_source_in_args_reaches_handler(
    mcp_runtime: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.handlers import pcb as pcb_mod

    got: dict[str, Any] = {}

    def fake(self: Any, ref: Any, args: dict[str, Any]) -> str:
        got.update(args)
        return "pinned"

    monkeypatch.setattr(pcb_mod, "resolve_live_slug_ref", lambda *a, **k: object())
    monkeypatch.setattr(pcb_mod.PcbHandler, "_op_pins", fake)
    out = _call(
        "put",
        {"kind": "pcb", "id": "b", "args": {"op": "pins", "source": "datasheet:x"}},
    )
    assert "does not accept" not in out, out
    assert got.get("source") == "datasheet:x"


def test_material_top_level_source_still_top_level(mcp_runtime: None) -> None:
    seen: list[dict[str, Any]] = []
    _recording_slim(seen)(kind="material", id="m1", source="paper:x")
    assert seen[0].get("source") == "paper:x"
    assert "source" not in (seen[0].get("args") or {})


def test_handler_declared_args_key_is_still_lifted(mcp_runtime: None) -> None:
    seen: list[dict[str, Any]] = []
    _recording_slim(seen)(kind="material", id="m1", args={"source": "paper:x"})
    assert seen[0].get("source") == "paper:x"
    # a key the handler does not declare stays in args
    seen.clear()
    _recording_slim(seen)(kind="pcb", id="b", args={"op": "pins", "source": "s"})
    assert "source" not in seen[0]
    assert seen[0]["args"]["source"] == "s"


@pytest.mark.parametrize("key", ["k", "limit"])
def test_top_level_size_alias_promotes_to_page_size(
    key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Top-level ``k``/``limit`` are verb-consumed: the slim layer
    promotes them to the full function (never into ``args=``), which
    maps them onto ``page_size`` (gr530420)."""
    seen: dict[str, Any] = {}

    def capture(verb: str, payload: dict[str, Any], ctx: object = None) -> str:
        seen.update(payload)
        return "ok"

    monkeypatch.setattr(tools_core, "_dispatch", capture)
    out = _call("search", {"kind": "finding", "q": "nanobuds", key: 3})
    assert seen["page_size"] == 3
    assert "__extras__" not in seen or key not in seen["__extras__"]
    assert f"interpreted: '{key}' read as 'page_size'" in out


@pytest.mark.parametrize("key", ["k", "limit", "offset"])
def test_args_nested_size_alias_refuses_before_dispatch(
    key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("args-nested alias reached dispatch")

    monkeypatch.setattr(tools_core, "_dispatch", unexpected)
    out = _call("search", {"kind": "finding", "q": "nanobuds", "args": {key: 3}})
    assert "[error:BadInput]" in out, out
    assert "page_size" in out


def test_non_skill_kinds_refuses_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("kinds= reached dispatch")

    monkeypatch.setattr(tools_core, "_dispatch", unexpected)
    out = _call(
        "search",
        {"q": "nanobuds", "kinds": ["memory", "finding"]},
    )
    assert "[error:BadInput]" in out, out
    assert "search(kind='memory,finding'" in out


def test_canonical_sort_and_page_size_reach_full_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def capture(verb: str, payload: dict[str, Any], ctx: object = None) -> str:
        seen.update(payload)
        return "ok"

    monkeypatch.setattr(tools_core, "_dispatch", capture)
    out = _call(
        "search",
        {"kind": "finding", "q": "nanobuds", "sort": "recency", "page_size": 3},
    )
    assert out == "ok"
    assert seen["sort"] == "recency"
    assert seen["page_size"] == 3


def test_skill_kinds_filter_still_reaches_full_search(
    mcp_runtime: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def capture(verb: str, payload: dict[str, Any], ctx: object = None) -> str:
        seen.update(payload)
        return "ok"

    monkeypatch.setattr(tools_core, "_dispatch", capture)
    out = _call(
        "search",
        {"kind": "skill", "q": "memory", "args": {"kinds": "gripe"}},
    )
    assert out == "ok"
    assert seen["kinds"] == "gripe"


def test_search_page_continuation_refuses_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("page continuation reached dispatch")

    monkeypatch.setattr(tools_core, "_dispatch", unexpected)
    out = _call("search", {"kind": "paper", "q": "nanobuds", "page": 2})
    assert "[error:BadInput]" in out, out
    assert "search no longer paginates" in out
    assert "more specific terms" in out

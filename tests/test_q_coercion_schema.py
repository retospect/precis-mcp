"""Pins the wire-level ``inputSchema`` + pydantic validation for ``q=``
on ``get``/``search`` after gr650516 (same root as gr330034/gr261385).

Repro: some MCP client bridges auto-parse a JSON-shaped ``q=`` STRING
into a dict/list before the tool's declared ``q: str`` schema ever sees
it — pydantic then rejects the call with ``Input should be a valid
string ... input_type=list``. ``get(kind='provenance', view='verify',
q='[{"doi": …}]')`` — the exact spelling precis-provenance-help
documents — was uncallable from such a client: every strict-JSON q was
coerced away, and every quoting workaround failed the tool's own
``json.loads`` on the other side.

The fix widens ``q=`` on ``get``/``search`` to ``str | dict | list``
and re-serializes the coerced shape back into the documented ``str``
before dispatch (``tools.core._coerce_str_arg``), mirroring ``text=``'s
``_coerce_text_body``. The ``search`` slim verb inherits the widened
annotation from the full signature it copies. The OSS tool-loop bridge
(``precis_tools._json_schema_for``) keeps advertising ``string`` for the
str-led union — the container arm tolerates coercion, it is not the
documented contract.

These tests pin:

- the wire ``inputSchema`` carries the union at *property* level
  (``anyOf`` inside a property is legal; a root-level ``anyOf``/
  ``oneOf``/``allOf`` would make Anthropic's ``/v1/messages`` API reject
  the whole ``tools`` array — see ``server.py::_install_edit_schema_constraints``);
- pydantic's argument models (``fn_metadata.arg_model`` — the exact
  models FastMCP validates incoming calls against, the full ``get``
  model and the slim ``search`` twin alike) accept a coerced list or
  dict for ``q=`` and still accept an ordinary string;
- over the real ``server.mcp.call_tool`` path, a coerced container
  reaches ``_dispatch`` as the re-serialized JSON string — byte-equal
  to what ``json.dumps`` produces — while a plain string passes
  through untouched;
- the OSS tool-loop schema mapper keeps advertising ``q``/``text`` as
  ``{"type": "string"}``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from precis import server
from precis.tools import core as tools_core
from precis.utils.llm.precis_tools import _json_schema_for


def _tool_schema(name: str) -> dict[str, Any]:
    tool = server.mcp._tool_manager.get_tool(name)
    assert tool is not None, f"{name!r} tool missing from FastMCP manager"
    return tool.parameters


def _q_property(name: str) -> dict[str, Any]:
    schema = _tool_schema(name)
    prop = schema.get("properties", {}).get("q")
    assert isinstance(prop, dict), f"{name!r} schema has no 'q' property: {schema!r}"
    return prop


@pytest.mark.parametrize("verb", ["get", "search"])
def test_q_schema_is_property_level_union_of_str_dict_list(verb: str) -> None:
    prop = _q_property(verb)
    any_of = prop.get("anyOf")
    assert any_of is not None, (
        f"expected a property-level anyOf on {verb}.q, got {prop!r}"
    )
    types = {branch.get("type") for branch in any_of}
    assert {"string", "object", "array", "null"} <= types, any_of


@pytest.mark.parametrize("verb", ["get", "search"])
def test_schema_root_has_no_union_keywords(verb: str) -> None:
    """The widened ``q=`` union must stay property-level — a root-level
    ``anyOf``/``oneOf``/``allOf`` on the inputSchema would make Anthropic's
    ``/v1/messages`` API reject the entire ``tools`` array."""
    schema = _tool_schema(verb)
    for keyword in ("oneOf", "allOf", "anyOf"):
        assert keyword not in schema, (
            f"top-level {keyword!r} present in {verb} inputSchema — this breaks "
            "every tool call over the official Anthropic API"
        )


@pytest.mark.parametrize("verb", ["get", "search"])
def test_arg_model_accepts_str_dict_and_list_for_q(verb: str) -> None:
    """The reported repro: the exact pydantic model FastMCP validates an
    incoming call against must accept a coerced list/dict — not raise
    ``Input should be a valid string ... input_type=list`` — AND still
    accept the documented JSON string untouched."""
    tool = server.mcp._tool_manager.get_tool(verb)
    assert tool is not None
    arg_model = tool.fn_metadata.arg_model

    for coerced in ([{"doi": "10.x/y"}], {"doi": "10.x/y"}):
        parsed = arg_model.model_validate({"kind": "provenance", "q": coerced})
        assert parsed.model_dump()["q"] == coerced

    as_string = '[{"doi":"10.x/y","title":"t"}]'
    parsed = arg_model.model_validate({"kind": "provenance", "q": as_string})
    assert parsed.model_dump()["q"] == as_string


def _call(name: str, arguments: dict[str, Any]) -> str:
    result = asyncio.run(server.mcp.call_tool(name, arguments))
    if isinstance(result, tuple):  # (content, structured)
        result = result[0]
    if isinstance(result, list):
        return "".join(getattr(b, "text", "") for b in result)
    return str(result)


@pytest.mark.parametrize("verb", ["get", "search"])
def test_coerced_q_reaches_dispatch_as_the_json_string(
    verb: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over the real FastMCP call path: a client-side-coerced list/dict
    ``q=`` must reach ``_dispatch`` re-serialized to the JSON string the
    caller authored — what every kind's ``get``/``search`` parses."""
    seen: dict[str, Any] = {}

    def capture(verb_name: str, payload: dict[str, Any], ctx: object = None) -> str:
        seen.update(payload)
        return "ok"

    monkeypatch.setattr(tools_core, "_dispatch", capture)
    bib = [{"doi": "10.x/y", "title": "t", "year": 2020}]
    out = _call(verb, {"kind": "provenance", "view": "verify", "q": bib})
    assert out == "ok"
    assert seen["q"] == json.dumps(bib)


@pytest.mark.parametrize("verb", ["get", "search"])
def test_json_string_q_round_trips_as_json(
    verb: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that did NOT coerce (the documented spelling) sends the
    JSON batch as a plain string. FastMCP's ``pre_parse_json`` parses it
    for the container arm of the union and ``_coerce_str_arg``
    re-serializes — the payload is the same JSON modulo whitespace,
    which is all a ``json.loads`` consumer can ask for."""
    seen: dict[str, Any] = {}

    def capture(verb_name: str, payload: dict[str, Any], ctx: object = None) -> str:
        seen.update(payload)
        return "ok"

    monkeypatch.setattr(tools_core, "_dispatch", capture)
    q = '[{"doi":"10.x/y","title":"t","authors":["Smith"],"year":2020}]'
    out = _call(verb, {"kind": "provenance", "view": "verify", "q": q})
    assert out == "ok"
    assert json.loads(seen["q"]) == json.loads(q)


@pytest.mark.parametrize("verb", ["get", "search"])
def test_non_json_string_q_passes_through_byte_identical(
    verb: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ordinary prose/DOI query (not JSON-shaped, the common case)
    skips ``pre_parse_json`` and the container branch alike — the
    payload is byte-identical to what the client sent."""
    seen: dict[str, Any] = {}

    def capture(verb_name: str, payload: dict[str, Any], ctx: object = None) -> str:
        seen.update(payload)
        return "ok"

    monkeypatch.setattr(tools_core, "_dispatch", capture)
    for q in ("10.1038/nature05095", "mof co2 adsorption", "42", '"quoted"', "true"):
        out = _call(verb, {"kind": "provenance", "q": q})
        assert out == "ok"
        assert seen["q"] == q, (q, seen["q"])


def test_coerce_str_arg_unit() -> None:
    assert tools_core._coerce_str_arg(None) is None
    assert tools_core._coerce_str_arg("10.x/y") == "10.x/y"
    assert tools_core._coerce_str_arg([{"a": 1}]) == '[{"a": 1}]'
    assert tools_core._coerce_str_arg({"a": 1}) == '{"a": 1}'


def test_oss_bridge_schema_keeps_q_and_text_as_string() -> None:
    """``_json_schema_for`` must not let the tolerated dict/list arm of
    the union flip the advertised type to ``object`` — the documented
    contract for ``q=``/``text=`` is the string."""
    ann = "str | dict[str, Any] | list[Any] | None"
    assert _json_schema_for({"annotation": ann}) == {"type": "string"}
    # Pure dict params still advertise the container shape.
    assert _json_schema_for({"annotation": "dict[str, Any] | None"}) == {
        "type": "object",
        "additionalProperties": True,
    }

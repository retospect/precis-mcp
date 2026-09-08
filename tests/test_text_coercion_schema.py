"""Pins the wire-level ``inputSchema`` + pydantic validation for ``text=``
on ``put``/``edit`` after gr330034 (same root as gr261385).

Repro: some MCP client bridges auto-parse a JSON-shaped ``text=`` STRING
into a dict/list before the tool's declared ``text: str`` schema ever sees
it — pydantic then rejects the call with ``Input should be a valid string
... input_type=dict``. ``structure``'s only documented authoring entry
point is ``put(kind='structure', text=<JSON>)``, so the coercion made the
kind uninvokable from those clients (a non-JSON string, e.g. cad's line
language, passed through fine — only JSON-shaped bodies tripped it).

The fix widens ``text=`` on ``put``/``edit`` (and the ``command`` profile's
``precis(command, text=)``) to ``str | dict | list`` and re-serializes the
coerced shape back into the documented ``str`` before dispatch
(``tools.core._coerce_text_body``). These tests pin:

- the wire ``inputSchema`` stays API-legal (``anyOf`` is fine at the
  *property* level — Anthropic's ``/v1/messages`` API only rejects
  ``oneOf``/``allOf``/``anyOf`` at the *root* of ``input_schema``, per
  ``test_edit_schema.py`` / ``server.py::_install_edit_schema_constraints``);
- the ``_install_edit_schema_constraints`` mode-coupling description
  mutation still lands on ``text`` even though its schema is now a union
  (not a flat ``{"type": "string"}``);
- pydantic's auto-generated argument model (``fn_metadata.arg_model`` —
  ``putArguments`` / ``editArguments``, the exact model FastMCP validates
  incoming tool calls against) accepts both a dict and a string for
  ``text=``.
"""

from __future__ import annotations

from typing import Any

from precis import server


def _tool_schema(name: str) -> dict[str, Any]:
    tool = server.mcp._tool_manager.get_tool(name)
    assert tool is not None, f"{name!r} tool missing from FastMCP manager"
    return tool.parameters


def _text_property(name: str) -> dict[str, Any]:
    schema = _tool_schema(name)
    prop = schema.get("properties", {}).get("text")
    assert isinstance(prop, dict), f"{name!r} schema has no 'text' property: {schema!r}"
    return prop


def test_put_text_schema_is_property_level_union_of_str_dict_list() -> None:
    prop = _text_property("put")
    any_of = prop.get("anyOf")
    assert any_of is not None, (
        f"expected a property-level anyOf on put.text, got {prop!r}"
    )
    types = {branch.get("type") for branch in any_of}
    assert {"string", "object", "array", "null"} <= types, any_of


def test_edit_text_schema_is_property_level_union_of_str_dict_list() -> None:
    prop = _text_property("edit")
    any_of = prop.get("anyOf")
    assert any_of is not None, (
        f"expected a property-level anyOf on edit.text, got {prop!r}"
    )
    types = {branch.get("type") for branch in any_of}
    assert {"string", "object", "array", "null"} <= types, any_of


def test_put_schema_root_has_no_union_keywords() -> None:
    """The widened ``text=`` union must stay property-level — a root-level
    ``anyOf``/``oneOf``/``allOf`` on ``put``'s inputSchema would make
    Anthropic's ``/v1/messages`` API reject the entire ``tools`` array."""
    schema = _tool_schema("put")
    for keyword in ("oneOf", "allOf", "anyOf"):
        assert keyword not in schema, (
            f"top-level {keyword!r} present in put inputSchema — this breaks "
            "every tool call over the official Anthropic API"
        )


def test_edit_schema_root_still_has_no_union_keywords_after_text_widening() -> None:
    """Companion to ``test_edit_schema.py``'s equivalent test — re-pinned
    here because this fix touches ``edit``'s ``text`` annotation directly
    and a regression here would be easy to miss if only the put-side test
    existed."""
    schema = _tool_schema("edit")
    for keyword in ("oneOf", "allOf", "anyOf"):
        assert keyword not in schema, (
            f"top-level {keyword!r} present in edit inputSchema — this breaks "
            "every tool call over the official Anthropic API"
        )


def test_edit_schema_text_mode_coupling_description_survives_widening() -> None:
    """``_install_edit_schema_constraints`` appends the per-mode coupling
    note to ``text``'s ``description`` — must still work now that ``text``'s
    schema is an ``anyOf`` union instead of a flat ``{"type": "string"}``."""
    prop = _text_property("edit")
    desc = prop.get("description", "")
    assert "find-replace" in desc and "move=" in desc, (
        f"edit.text description lost the mode-coupling note after the "
        f"str|dict|list widening; got {desc!r}"
    )


def test_put_arg_model_accepts_both_str_and_dict_for_text() -> None:
    """The exact pydantic model FastMCP validates an incoming ``put`` call
    against (``fn_metadata.arg_model``) must accept a coerced dict — not
    raise ``Input should be a valid string ... input_type=dict`` — AND
    still accept an ordinary string untouched by any client-side bridge."""
    tool = server.mcp._tool_manager.get_tool("put")
    assert tool is not None
    arg_model = tool.fn_metadata.arg_model

    coerced = arg_model.model_validate(
        {"kind": "structure", "text": {"cell": {"a": 1}}}
    )
    assert coerced.model_dump()["text"] == {"cell": {"a": 1}}

    uncoerced = arg_model.model_validate({"kind": "structure", "text": "{}"})
    assert uncoerced.model_dump()["text"] == "{}"


def test_edit_arg_model_accepts_both_str_and_dict_for_text() -> None:
    tool = server.mcp._tool_manager.get_tool("edit")
    assert tool is not None
    arg_model = tool.fn_metadata.arg_model

    coerced = arg_model.model_validate(
        {"kind": "structure", "id": "x", "text": {"ops": []}}
    )
    assert coerced.model_dump()["text"] == {"ops": []}

    uncoerced = arg_model.model_validate({"kind": "structure", "id": "x", "text": "{}"})
    assert uncoerced.model_dump()["text"] == "{}"

"""Pins the wire-level ``inputSchema`` of the ``edit`` tool.

The MCP critic (2026-05-03) flagged that pydantic's auto-generated
schema marks ``text``, ``find`` and ``where`` as optional, which
tells small models they can omit those kwargs. Runtime validation
still catches the omission via ``BadInput``, but the schema-level
lie costs a retry loop on 7B / 8B callers that trust the declared
``required`` array.

``src/precis/server.py::_install_edit_schema_constraints`` rewrites
the schema at import time to encode the per-mode coupling:

- ``text`` is top-level required (true for every wire mode — schema
  ``required`` array is the field small models read first).
- ``find`` and ``where`` are mode-conditional; the coupling is
  encoded in the ``description`` text of those properties (and of
  ``mode``) so schema-reading clients still get the hint.

The old design used ``allOf`` + ``if/then`` to encode the
mode-conditional coupling at the JSON-Schema level. That broke
Anthropic's ``/v1/messages`` API, which rejects
``oneOf``/``allOf``/``anyOf`` at the root of
``tools[].custom.input_schema`` with a 400 and blocks every tool
call. We dropped to description-only encoding plus the runtime
``BadInput`` safety net so the surface stays usable with the
official API.

These tests lock the new shape so a future refactor that
re-introduces a top-level union keyword fails loudly in CI.
"""

from __future__ import annotations

from typing import Any

from precis import server


def _edit_schema() -> dict[str, Any]:
    tool = server.mcp._tool_manager.get_tool("edit")
    assert tool is not None, "edit tool missing from FastMCP manager"
    return tool.parameters


def test_edit_schema_lists_text_as_required() -> None:
    """``text`` is required on every wire-level mode; the top-level
    ``required`` array must advertise it so small models emit it on
    the first try rather than looping on BadInput."""
    schema = _edit_schema()
    required = schema.get("required", [])
    assert "text" in required, (
        f"'text' must be top-level required; got required={required!r}"
    )


def test_edit_schema_has_no_top_level_union_keywords() -> None:
    """Anthropic's ``/v1/messages`` API rejects schemas whose root has
    ``oneOf``/``allOf``/``anyOf``. Any of those at the top level breaks
    every tool call (including unrelated tools, because the API
    validates the whole ``tools`` array). This test guards the floor.
    """
    schema = _edit_schema()
    for keyword in ("oneOf", "allOf", "anyOf"):
        assert keyword not in schema, (
            f"top-level {keyword!r} re-introduced in edit inputSchema; "
            "Anthropic's /v1/messages API will reject the entire tools "
            "array with a 400. Encode the constraint as property "
            "descriptions or rely on runtime BadInput instead."
        )


def test_edit_schema_find_description_advertises_mode_coupling() -> None:
    """The ``find`` property's description must call out that it is
    required in ``find-replace`` and ``insert`` modes. With the
    allOf-based enforcement gone, this is the principal in-schema
    signal small models will see for the coupling."""
    schema = _edit_schema()
    desc = (schema.get("properties", {}).get("find", {}) or {}).get(
        "description", ""
    )
    assert "find-replace" in desc and "insert" in desc, (
        f"`find` description must name modes that require it; got {desc!r}"
    )


def test_edit_schema_where_description_advertises_mode_coupling() -> None:
    """``where`` is required when ``mode='insert'``."""
    schema = _edit_schema()
    desc = (schema.get("properties", {}).get("where", {}) or {}).get(
        "description", ""
    )
    assert "insert" in desc, (
        f"`where` description must name mode='insert' as the trigger; got {desc!r}"
    )


def test_edit_schema_mode_description_enumerates_per_mode_required_args() -> None:
    """The ``mode`` property's description should table the per-mode
    required args (``find-replace`` → find=, text=; ``insert`` →
    find=, text=, where=; ``append``/``replace`` → text=). Small
    models reading the mode field surface should see this without
    having to fetch the help skill."""
    schema = _edit_schema()
    desc = (schema.get("properties", {}).get("mode", {}) or {}).get(
        "description", ""
    )
    for token in ("find-replace", "insert", "append", "replace"):
        assert token in desc, (
            f"mode description must enumerate mode {token!r}; got {desc!r}"
        )


def test_idempotent_schema_install_does_not_duplicate_clauses() -> None:
    """Calling the installer twice must not duplicate the top-level
    ``text`` requirement or re-append the description suffixes.
    Guards against repeated module imports (e.g. under pytest with a
    reload plugin).
    """
    schema_before = _edit_schema()
    before_required = list(schema_before.get("required", []))
    before_props = {
        name: dict(schema_before.get("properties", {}).get(name, {}))
        for name in ("mode", "find", "where")
    }

    server._install_edit_schema_constraints(server.mcp)

    schema_after = _edit_schema()
    # `text` should appear exactly once.
    assert schema_after.get("required", []).count("text") == 1
    assert schema_after.get("required", []) == before_required, (
        "installer re-added `text` to required"
    )
    # Property descriptions should be stable across re-runs.
    for name, before_schema in before_props.items():
        after_schema = schema_after.get("properties", {}).get(name, {})
        assert after_schema.get("description") == before_schema.get(
            "description"
        ), (
            f"installer re-appended description suffix on {name!r}: "
            f"before={before_schema.get('description')!r} "
            f"after={after_schema.get('description')!r}"
        )

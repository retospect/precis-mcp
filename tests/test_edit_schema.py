"""Pins the wire-level ``inputSchema`` of the ``edit`` tool.

The MCP critic (2026-05-03) flagged that pydantic's auto-generated
schema marks ``text``, ``find`` and ``where`` as optional, which
tells small models they can omit those kwargs. Runtime validation
still catches the omission via ``BadInput``, but the schema-level
lie costs a retry loop on 7B / 8B callers that trust the declared
``required`` array.

``src/precis/server.py::_install_edit_schema_constraints`` rewrites
the schema at import time to encode the per-mode coupling:

- ``text`` is top-level required (true for every wire mode).
- ``find`` is required via ``allOf`` + ``if/then`` when
  ``mode in {'find-replace', 'insert'}`` or ``mode`` is omitted
  (pydantic default is 'find-replace').
- ``where`` is required when ``mode='insert'``.

These tests lock the schema shape so a future refactor that
reintroduces the lie fails loudly in CI.
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


def test_edit_schema_requires_find_when_mode_is_find_replace() -> None:
    """When ``mode='find-replace'`` is set explicitly, ``find`` must
    be required via ``allOf`` / ``if/then``."""
    schema = _edit_schema()
    all_of = schema.get("allOf", [])
    assert all_of, "edit schema must carry allOf clauses for per-mode coupling"
    # Find the clause whose `if` matches `mode='find-replace'` (or a
    # multi-enum containing it) and `then` requires `find`.
    found = False
    for clause in all_of:
        if_schema = clause.get("if", {})
        mode_prop = if_schema.get("properties", {}).get("mode", {})
        values = mode_prop.get("enum") or [mode_prop.get("const")]
        if "find-replace" in values and "find" in clause.get("then", {}).get(
            "required", []
        ):
            found = True
            break
    assert found, (
        "no allOf/if-then clause requires `find` when mode='find-replace'; "
        f"allOf={all_of!r}"
    )


def test_edit_schema_requires_find_when_mode_is_insert() -> None:
    """Same coverage for mode='insert'."""
    schema = _edit_schema()
    all_of = schema.get("allOf", [])
    found = False
    for clause in all_of:
        if_schema = clause.get("if", {})
        mode_prop = if_schema.get("properties", {}).get("mode", {})
        values = mode_prop.get("enum") or [mode_prop.get("const")]
        if "insert" in values and "find" in clause.get("then", {}).get("required", []):
            found = True
            break
    assert found, "no allOf/if-then clause requires `find` when mode='insert'"


def test_edit_schema_requires_where_when_mode_is_insert() -> None:
    """``where`` is required for anchored inserts."""
    schema = _edit_schema()
    all_of = schema.get("allOf", [])
    found = False
    for clause in all_of:
        if_schema = clause.get("if", {})
        mode_prop = if_schema.get("properties", {}).get("mode", {})
        values = mode_prop.get("enum") or [mode_prop.get("const")]
        if "insert" in values and "where" in clause.get("then", {}).get("required", []):
            found = True
            break
    assert found, "no allOf/if-then clause requires `where` when mode='insert'"


def test_edit_schema_requires_find_when_mode_is_omitted() -> None:
    """When the caller omits ``mode``, pydantic fills it with
    ``'find-replace'`` — so ``find`` is still required."""
    schema = _edit_schema()
    all_of = schema.get("allOf", [])
    # Look for a clause whose ``if`` says mode is absent.
    found = False
    for clause in all_of:
        if_schema = clause.get("if", {})
        if if_schema.get("not", {}).get("required") == ["mode"]:
            if "find" in clause.get("then", {}).get("required", []):
                found = True
                break
    assert found, (
        "no allOf/if-then clause requires `find` when mode is omitted; "
        "small models default to mode='find-replace' but a schema with "
        "no coverage for the absent-mode case leaves them free to omit "
        "`find` too."
    )


def test_idempotent_schema_install_does_not_duplicate_clauses() -> None:
    """Calling the installer twice must not duplicate clauses or the
    top-level `text` requirement. Guards against repeated module
    imports (e.g. under pytest with a reload plugin).
    """
    schema_before = _edit_schema()
    before_required = list(schema_before.get("required", []))
    before_allof = list(schema_before.get("allOf", []))

    server._install_edit_schema_constraints(server.mcp)

    schema_after = _edit_schema()
    # `text` should appear exactly once.
    assert schema_after.get("required", []).count("text") == 1
    # allOf should not grow every call. The installer appends the
    # fixed set of conditions each call; idempotency means a second
    # call must not add duplicates.
    assert schema_after.get("allOf", []) == before_allof, (
        "installer is not idempotent; re-running doubled the allOf "
        f"clauses (before={len(before_allof)} after={len(schema_after.get('allOf', []))})"
    )
    assert schema_after.get("required", []) == before_required, (
        "installer re-added `text` to required"
    )

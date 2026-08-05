"""Pins the wire-level ``inputSchema`` of the ``edit`` tool.

The MCP critic (2026-05-03) flagged that pydantic's auto-generated
schema marks ``text``, ``find`` and ``where`` as optional, which
tells small models they can omit those kwargs. Runtime validation
still catches the omission via ``BadInput``, but the schema-level
lie costs a retry loop on 7B / 8B callers that trust the declared
``required`` array.

``src/precis/server.py::_install_edit_schema_constraints`` rewrites
the schema at import time to encode the per-mode coupling:

- ``text``, ``find`` and ``where`` are all mode-conditional — none is
  top-level ``required``. ``text`` is required on every text-mutation
  mode (``find-replace``/``insert``/``append``/``replace``) but NOT on
  the structural ops (``move=``, ``table=``, ``cell=``, ``review=``,
  ``authors=``, ``sub=``, …) — gr192827 item 1 found that forcing
  ``text`` unconditionally required blocked a plain ``edit(move=...)``
  call for a strict-schema client (it demanded a dummy
  ``text=None``). The coupling for all three is encoded in the
  ``description`` text of those properties (and of ``mode``) so
  schema-reading clients still get the hint.

The old design used ``allOf`` + ``if/then`` to encode the
mode-conditional coupling at the JSON-Schema level. That broke
Anthropic's ``/v1/messages`` API, which rejects
``oneOf``/``allOf``/``anyOf`` at the root of
``tools[].custom.input_schema`` with a 400 and blocks every tool
call. We dropped to description-only encoding plus the runtime
``BadInput`` safety net so the surface stays usable with the
official API. A flat top-level ``required`` array can't express
"required unless one of these other fields is set" either, which is
why ``text`` isn't there despite being required on the common path.

These tests lock the new shape so a future refactor that
re-introduces a top-level union keyword (or a blanket ``text``
requirement that blocks structural ops again) fails loudly in CI.
"""

from __future__ import annotations

from typing import Any

from precis import server


def _edit_schema() -> dict[str, Any]:
    tool = server.mcp._tool_manager.get_tool("edit")
    assert tool is not None, "edit tool missing from FastMCP manager"
    return tool.parameters


def test_edit_schema_does_not_force_text_required() -> None:
    """``text`` must NOT be top-level required — a growing family of
    structural ops (``move=``, ``table=``, ``cell=``, ``review=``,
    ``authors=``, ``sub=``, …) never touch ``text=`` at all, and a
    blanket requirement blocks those calls for a strict-schema client
    unless it pads a dummy ``text=None`` (gr192827 item 1)."""
    schema = _edit_schema()
    required = schema.get("required", [])
    assert "text" not in required, (
        f"'text' must not be top-level required — it would block "
        f"move=/table=/cell=/review=/authors=/sub= calls; "
        f"got required={required!r}"
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


def test_edit_schema_text_description_advertises_mode_coupling() -> None:
    """The ``text`` property's description must call out which modes
    require it, and must explicitly name at least one structural op
    (``move=``) that does NOT need it — the schema-level signal that
    replaces the removed blanket ``required`` entry (gr192827 item 1)."""
    schema = _edit_schema()
    desc = (schema.get("properties", {}).get("text", {}) or {}).get("description", "")
    assert "find-replace" in desc and "insert" in desc, (
        f"`text` description must name the modes that require it; got {desc!r}"
    )
    assert "move=" in desc, (
        f"`text` description must call out a structural op that does NOT "
        f"require text=; got {desc!r}"
    )


def test_edit_schema_find_description_advertises_mode_coupling() -> None:
    """The ``find`` property's description must call out that it is
    required in ``find-replace`` and ``insert`` modes. With the
    allOf-based enforcement gone, this is the principal in-schema
    signal small models will see for the coupling."""
    schema = _edit_schema()
    desc = (schema.get("properties", {}).get("find", {}) or {}).get("description", "")
    assert "find-replace" in desc and "insert" in desc, (
        f"`find` description must name modes that require it; got {desc!r}"
    )


def test_edit_schema_where_description_advertises_mode_coupling() -> None:
    """``where`` is required when ``mode='insert'``."""
    schema = _edit_schema()
    desc = (schema.get("properties", {}).get("where", {}) or {}).get("description", "")
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
    desc = (schema.get("properties", {}).get("mode", {}) or {}).get("description", "")
    for token in ("find-replace", "insert", "append", "replace"):
        assert token in desc, (
            f"mode description must enumerate mode {token!r}; got {desc!r}"
        )


def test_idempotent_schema_install_does_not_duplicate_clauses() -> None:
    """Calling the installer twice must not duplicate ``required`` or
    re-append the description suffixes. Guards against repeated module
    imports (e.g. under pytest with a reload plugin).
    """
    schema_before = _edit_schema()
    before_required = list(schema_before.get("required", []))
    before_props = {
        name: dict(schema_before.get("properties", {}).get(name, {}))
        for name in ("mode", "find", "where", "text")
    }

    server._install_edit_schema_constraints(server.mcp)

    schema_after = _edit_schema()
    assert schema_after.get("required", []) == before_required, (
        "installer mutated required on a second install"
    )
    # Property descriptions should be stable across re-runs.
    for name, before_schema in before_props.items():
        after_schema = schema_after.get("properties", {}).get(name, {})
        assert after_schema.get("description") == before_schema.get("description"), (
            f"installer re-appended description suffix on {name!r}: "
            f"before={before_schema.get('description')!r} "
            f"after={after_schema.get('description')!r}"
        )

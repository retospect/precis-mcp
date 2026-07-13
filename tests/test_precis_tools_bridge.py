"""Tests for :mod:`precis.utils.llm.precis_tools` — the verb-registry bridge.

The schema mapping + executor injection are pure (no DB); the one
registry-backed test just reads ``TOOL_REGISTRY`` (import-only, no runtime).
"""

from __future__ import annotations

from typing import Any

from precis.utils.llm.precis_tools import (
    _json_schema_for,
    _spec_from_registry,
    precis_tool_specs,
    runtime_executor,
)

# ── annotation → JSON schema ───────────────────────────────────────────


def test_json_schema_scalar_string() -> None:
    assert _json_schema_for({"annotation": "str | None"}) == {"type": "string"}


def test_json_schema_id_union_collapses_to_string() -> None:
    # str | int | None → string (precis coerces "42"); models pass strings.
    assert _json_schema_for({"annotation": "str | int | None"}) == {"type": "string"}


def test_json_schema_list() -> None:
    got = _json_schema_for({"annotation": "list[str] | None", "is_list": True})
    assert got == {"type": "array", "items": {"type": "string"}}


def test_json_schema_dict() -> None:
    got = _json_schema_for({"annotation": "dict[str, Any] | None"})
    assert got == {"type": "object", "additionalProperties": True}


def test_json_schema_int_and_bool() -> None:
    assert _json_schema_for({"annotation": "int"}) == {"type": "integer"}
    assert _json_schema_for({"annotation": "bool"}) == {"type": "boolean"}


def test_json_schema_carries_description() -> None:
    got = _json_schema_for({"annotation": "str"}, description="the kind")
    assert got == {"type": "string", "description": "the kind"}


# ── registry → ToolSpec ────────────────────────────────────────────────


def test_spec_from_registry_builds_object_schema() -> None:
    info: dict[str, Any] = {
        "doc": "  read a ref  ",
        "parameters": {
            "kind": {"annotation": "str | None", "required": False},
            "id": {"annotation": "str | int | None", "required": True},
            "tags": {"annotation": "list[str]", "required": False, "is_list": True},
        },
        "cli_help": {"kind": "the kind axis"},
    }
    spec = _spec_from_registry("get", info)
    assert spec.name == "get"
    assert spec.description == "read a ref"  # doc stripped
    props = spec.parameters["properties"]
    assert props["kind"] == {"type": "string", "description": "the kind axis"}
    assert props["id"] == {"type": "string"}
    assert props["tags"] == {"type": "array", "items": {"type": "string"}}
    assert spec.parameters["required"] == ["id"]  # only the required arg


# ── executor injection ─────────────────────────────────────────────────


def test_runtime_executor_uses_injected_dispatch() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    def disp(name: str, args: dict[str, Any]) -> str:
        seen.append((name, args))
        return f"dispatched:{name}"

    execute = runtime_executor(dispatch=disp)
    out = execute("search", {"q": "x"})
    assert out == "dispatched:search"
    assert seen == [("search", {"q": "x"})]


# ── the live registry (import-only, no runtime/DB) ─────────────────────


def test_precis_tool_specs_covers_verbs_excludes_more() -> None:
    specs = precis_tool_specs()
    names = {s.name for s in specs}
    assert {"get", "search", "put", "edit", "delete", "tag", "link"} <= names
    assert "more" not in names  # pagination excluded by default
    for s in specs:  # every advertised tool has an object-typed schema
        assert s.parameters["type"] == "object"
        assert isinstance(s.description, str)

"""gr499041: per-session suppression of repeated hint text."""

from __future__ import annotations

import json
from typing import Any

from precis import serve_ledger
from precis.utils.hint_ledger import _clear_for_tests
from precis.utils.next_block import render_next_section


def _hint() -> str:
    return render_next_section([("get(kind='gripe', id=1)", "open the gripe")])


def test_sessionless_always_shows() -> None:
    _clear_for_tests()
    assert _hint() == _hint() != ""


def test_identical_hint_suppressed_within_one_session() -> None:
    _clear_for_tests()
    with serve_ledger.session_scope(object()):
        first, second, third = _hint(), _hint(), _hint()
    assert first != "" and second == "" and third == ""


def test_different_sessions_each_see_it() -> None:
    _clear_for_tests()
    with serve_ledger.session_scope(object()):
        a1 = _hint()
    with serve_ledger.session_scope(object()):
        b1 = _hint()
    assert a1 != "" and b1 != ""


def test_different_text_in_same_session_still_shows() -> None:
    _clear_for_tests()
    with serve_ledger.session_scope(object()):
        one = render_next_section([("get(kind='gripe', id=1)", "open one")])
        two = render_next_section([("get(kind='gripe', id=2)", "open two")])
    assert one != "" and two != ""


def test_schema_compression() -> None:
    from precis.server import _compress_tool_schema

    schema: dict[str, Any] = {
        "properties": {
            "kind": {
                "title": "Kind",
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": None,
            },
            "id": {"title": "Id", "type": "integer", "description": "keep me"},
            "args": {"anyOf": [{"type": "object"}, {"type": "null"}], "default": None},
        }
    }
    _compress_tool_schema(schema)
    kind = schema["properties"]["kind"]
    assert "title" not in kind and "default" not in kind
    assert kind["type"] == ["string", "null"]
    assert schema["properties"]["id"]["description"] == "keep me"
    assert schema["properties"]["args"]["type"] == ["object", "null"]
    blob = json.dumps(schema)
    assert "anyOf" not in blob


def test_schema_compression_leaves_non_nullable_anyof() -> None:
    from precis.server import _compress_tool_schema

    schema = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
    _compress_tool_schema(schema)
    assert schema["anyOf"] == [{"type": "string"}, {"type": "integer"}]

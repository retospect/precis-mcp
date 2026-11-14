"""gr499041: per-session suppression of repeated hint text."""

from __future__ import annotations

import json
import re
from typing import Any

from precis import serve_ledger
from precis.runtime import PrecisRuntime
from precis.store import Store
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


def test_trust_footer_recipe_shown_once_per_session(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """The runnable consider call prints on the session's first trust
    footer only; the stamp and sha stay on every later one."""
    _clear_for_tests()
    mids: list[int] = []
    for text in ("first stamped note", "second stamped note"):
        out = runtime_with_store.dispatch("put", {"kind": "memory", "text": text})
        m = re.search(r"\bme(\d+)\b", out)
        assert m, out
        mids.append(int(m.group(1)))
    session = object()
    with serve_ledger.session_scope(session):
        first = runtime_with_store.dispatch("get", {"id": f"me{mids[0]}"})
        second = runtime_with_store.dispatch("get", {"id": f"me{mids[1]}"})
        again = runtime_with_store.dispatch("get", {"id": f"me{mids[0]}"})
    assert "mode='trust'" in first
    for later in (second, again):
        assert "trust:" in later
        assert " · sha " in later
        assert "mode='trust'" not in later
    # Sessionless callers always get the full runnable line.
    plain = runtime_with_store.dispatch("get", {"id": f"me{mids[1]}"})
    assert "mode='trust'" in plain


def test_trust_footer_recipe_once_per_session_across_kinds(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """The recipe is one template for every kind — a session that just
    saw it on a memory does not see it again on a gripe (gr499041)."""
    _clear_for_tests()
    out = runtime_with_store.dispatch("put", {"kind": "memory", "text": "a note"})
    m = re.search(r"\bme(\d+)\b", out)
    assert m, out
    mid = int(m.group(1))
    out = runtime_with_store.dispatch("put", {"kind": "gripe", "text": "a gripe"})
    g = re.search(r"\bid=(\d+)\b", out)
    assert g, out
    gid = int(g.group(1))
    with serve_ledger.session_scope(object()):
        first = runtime_with_store.dispatch("get", {"id": f"me{mid}"})
        second = runtime_with_store.dispatch("get", {"kind": "gripe", "id": gid})
    assert "mode='trust'" in first
    assert "trust:" in second and " · sha " in second
    assert "mode='trust'" not in second


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

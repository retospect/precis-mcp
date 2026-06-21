"""LLM transcript parsing + viewer (plan_tick stream-json → readable log)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from precis_web.routes.tasks import _parse_transcript


def test_parse_transcript_into_turns() -> None:
    raw = (
        '{"type":"system","subtype":"init"}\n'
        '{"type":"assistant","message":{"content":['
        '{"type":"text","text":"thinking"},'
        '{"type":"tool_use","name":"put","input":{"kind":"todo"}}]}}\n'
        '{"type":"user","message":{"content":['
        '{"type":"tool_result","content":"ok done"}]}}\n'
        '{"type":"result","result":"all set"}'
    )
    turns = _parse_transcript(raw)
    assert [t["role"] for t in turns] == ["assistant", "tool_result", "result"]
    assert (
        turns[0]["tools"][0]["name"] == "put"
        and "todo" in turns[0]["tools"][0]["input"]
    )
    assert "ok done" in turns[1]["text"]
    assert turns[2]["text"] == "all set"


def test_parse_transcript_is_tolerant() -> None:
    assert _parse_transcript("garbage\n{bad json\n\n") == []
    assert _parse_transcript("") == []


def test_transcript_route_missing_is_graceful(client) -> None:
    r = client.get("/tasks/999999/transcript")
    assert r.status_code == 200
    assert "No transcript" in r.text

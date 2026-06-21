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


def test_tool_result_decodes_unicode_and_newlines() -> None:
    """A tool_result with list-of-text-blocks content renders decoded
    glyphs (¶, ₂) and real newlines — not \\u00b6 / literal \\n."""
    import json

    raw = (
        '{"type":"user","message":{"content":['
        + json.dumps(
            {
                "type": "tool_result",
                "content": [{"type": "text", "text": "¶vBFGDc CO₂\nnext line"}],
            }
        )
        + "]}}"
    )
    turns = _parse_transcript(raw)
    assert len(turns) == 1 and turns[0]["role"] == "tool_result"
    text = turns[0]["text"]
    assert "¶vBFGDc" in text and "CO₂" in text  # decoded glyphs
    assert "\n" in text and "\\u00b6" not in text  # real newline, no escape

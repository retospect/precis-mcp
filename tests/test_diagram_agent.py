"""The agentic drawer (``precis.diagram.agent``) — L3 of the diagram-propose
design: a tool-using ``claude_fn`` routed through the ADR-0046 seam. No real
``claude -p`` here; the seam ``dispatch`` is stubbed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import precis.utils.llm.router as router
from precis.diagram.agent import build_agentic_claude_fn, extract_reply_json

# ── reply extraction ─────────────────────────────────────────────────


def test_extract_pure_json() -> None:
    assert extract_reply_json('{"reply": "hi", "svg": "<svg/>"}') == {
        "reply": "hi",
        "svg": "<svg/>",
    }


def test_extract_fenced_json() -> None:
    text = 'Here is my drawing:\n```json\n{"reply": "done", "svg": "<svg/>"}\n```\n'
    assert extract_reply_json(text) == {"reply": "done", "svg": "<svg/>"}


def test_extract_prose_wrapped_last_object() -> None:
    text = 'I searched dc11 and dc12. Final answer: {"reply": "ok", "vocab": "v"}'
    assert extract_reply_json(text) == {"reply": "ok", "vocab": "v"}


def test_extract_non_json_falls_back_to_reply() -> None:
    out = extract_reply_json("I could not finish drawing.")
    assert out == {"reply": "I could not finish drawing."}


def test_extract_empty() -> None:
    assert extract_reply_json("   ") == {}


# ── the agentic claude_fn ────────────────────────────────────────────


def test_agentic_fn_routes_with_tools_and_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _fake_dispatch(req: Any) -> Any:
        captured["req"] = req
        return SimpleNamespace(error=None, text='{"reply": "drew it", "svg": "<svg/>"}')

    monkeypatch.setattr(router, "dispatch", _fake_dispatch)
    fn = build_agentic_claude_fn(source="figure:test", mcp_config=None, max_turns=7)
    out = fn("## Current source\n<svg/>\n\nReply with ONE JSON object …")

    assert out == {"reply": "drew it", "svg": "<svg/>"}
    req = captured["req"]
    assert req.tools_needed is True  # the whole point of L3
    assert req.mcp_config is None
    assert req.max_turns == 7
    assert req.source == "figure:test"
    # the tool-use preamble is prepended ahead of the assembled turn prompt
    assert req.prompt.startswith("You are drawing WITH tools.")
    assert "## Current source" in req.prompt


def test_agentic_fn_raises_on_seam_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        router, "dispatch", lambda req: SimpleNamespace(error="quota exceeded", text="")
    )
    fn = build_agentic_claude_fn(mcp_config=None)
    with pytest.raises(RuntimeError, match="quota exceeded"):
        fn("prompt")

"""Turn-loop tests — DB-backed, model injected (no real claude)."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.figure.turn import build_prompt, run_turn
from precis.handlers.figure import FigureHandler

_GOOD = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<circle id="face" cx="50" cy="50" r="30" fill="green"/></svg>'
)


@pytest.fixture
def ref(store):
    fh = FigureHandler(hub=Hub(store=store))
    fh.put(id="m", title="M")
    return store.get_ref(kind="figure", id="m")


def _fixed(reply="ok", svg=None, vocab=None, notes=None):
    payload = {"reply": reply}
    if svg is not None:
        payload["svg"] = svg
    if vocab is not None:
        payload["vocab"] = vocab
    if notes is not None:
        payload["notes"] = notes
    return lambda prompt: dict(payload)


# ── build_prompt (pure) ──────────────────────────────────────────────────


def test_build_prompt_carries_the_parts():
    p = build_prompt(
        message="draw a face",
        svg="<svg/>",
        vocab="green circles are foos",
        notes="the face is <g id='face'>",
        findings=[],
        viewbox=(0.0, 0.0, 100.0, 100.0),
        skills="SKILLZ",
    )
    assert "draw a face" in p
    assert "green circles are foos" in p
    assert "id='face'" in p  # implementation notes carried
    assert "SKILLZ" in p
    assert '"reply"' in p  # the JSON contract
    assert '"notes"' in p  # the contract now asks for notes


def test_build_prompt_admonishes_updating_docs():
    # The floor guidance (present even if the skill fails to load) must tell
    # the model to keep the vocabulary high-level and updated.
    p = build_prompt(
        message="x",
        svg="<svg/>",
        vocab="",
        findings=[],
        viewbox=(0.0, 0.0, 100.0, 100.0),
    )
    low = p.lower()
    assert "update the vocab" in low
    assert "high-level" in low
    assert "short" in low  # keep the reply short


# ── run_turn ─────────────────────────────────────────────────────────────


def test_turn_applies_svg(store, ref):
    res = run_turn(store, ref, "draw a green face", claude_fn=_fixed(svg=_GOOD))
    assert res.changed
    assert "circle" in res.svg
    assert res.findings == []


def test_turn_persists_source(store, ref):
    run_turn(store, ref, "draw", claude_fn=_fixed(svg=_GOOD))
    fh = FigureHandler(hub=Hub(store=store))
    assert "circle" in fh.get(id="m").body


def test_turn_updates_vocab(store, ref):
    res = run_turn(
        store, ref, "note the convention", claude_fn=_fixed(vocab="foo=green")
    )
    assert res.vocab == "foo=green"  # returned for pane reload
    fh = FigureHandler(hub=Hub(store=store))
    assert "foo=green" in fh.get(id="m").body


def test_turn_updates_notes(store, ref):
    res = run_turn(
        store, ref, "record structure", claude_fn=_fixed(notes="face = g#face")
    )
    assert res.notes == "face = g#face"  # returned for pane reload
    fh = FigureHandler(hub=Hub(store=store))
    body = fh.get(id="m").body
    assert "Implementation notes" in body
    assert "face = g#face" in body


def test_turn_vocab_and_notes_are_separate_chunks(store, ref):
    run_turn(
        store,
        ref,
        "both",
        claude_fn=_fixed(vocab="a mascot", notes="head = circle#head"),
    )
    kinds = [c.chunk_kind for c in store.reading_order(ref.id, kind="figure")]
    assert kinds.count("figure_vocab") == 1
    assert kinds.count("figure_notes") == 1


def test_chat_only_turn_leaves_source(store, ref):
    res = run_turn(store, ref, "what do you think?", claude_fn=_fixed(reply="nice"))
    assert not res.changed
    assert res.reply == "nice"
    # source still the birth canvas
    assert "empty canvas" in res.svg


def test_turn_sanitizes_model_svg(store, ref):
    evil = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<script>steal()</script><rect x="1" y="1" width="2" height="2"/></svg>'
    )
    res = run_turn(store, ref, "draw", claude_fn=_fixed(svg=evil))
    assert "script" not in res.svg.lower()
    assert "rect" in res.svg


def test_turn_auto_heals_bad_svg(store, ref):
    calls = {"n": 0}

    def flaky(prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"reply": "oops", "svg": "<svg><rect></svg>"}  # malformed
        return {"reply": "fixed", "svg": _GOOD}

    res = run_turn(store, ref, "draw", claude_fn=flaky)
    assert res.healed
    assert res.changed
    assert "circle" in res.svg
    assert calls["n"] == 2


def test_turn_keeps_source_when_heal_fails(store, ref):
    def broken(prompt):
        return {"reply": "still broken", "svg": "<svg><rect></svg>"}

    res = run_turn(store, ref, "draw", claude_fn=broken)
    assert not res.changed  # never overwrote good source with broken
    assert res.healed


def test_turn_survives_model_exception(store, ref):
    def boom(prompt):
        raise RuntimeError("model down")

    res = run_turn(store, ref, "draw", claude_fn=boom)
    assert not res.changed
    # jargon-free, actionable — not a raw "claude -p exited 1"
    assert "couldn't reach" in res.reply.lower()
    assert "exited" not in res.reply.lower()


def test_turn_logs_persist_across_turns(store, ref):
    run_turn(store, ref, "one", claude_fn=_fixed(reply="a"))
    run_turn(store, ref, "two", claude_fn=_fixed(reply="b"))
    fh = FigureHandler(hub=Hub(store=store))
    assert "2 turn(s)" in fh.get(id="m").body

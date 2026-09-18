"""Tests for the tolerant JSON-object extractor (gr345336).

``extract_json_object`` got a ``strict=False`` second chance (permits an
unescaped raw control character inside a string value) after both strict
attempts fail — these regression-guard the two shapes that must KEEP
hard-failing despite that loosening: truncated/unbalanced input, and a
stray unmatched ``]`` after a string field (a real prod sampling glitch,
ref 341220) — neither is a control-character problem, so ``strict=False``
must not paper over either.
"""

from __future__ import annotations

from precis.utils.llm.json_reply import extract_json_object


def test_extract_json_object_unfenced_raw_newline_in_string() -> None:
    """A model reply with no code fence whose JSON is otherwise well-formed
    but has a literal (unescaped) newline inside a string value parses
    after the strict=False second chance — it was rejected outright
    before this fix."""
    text = '{"a": "line1\nline2", "b": 2}'
    assert extract_json_object(text) == {"a": "line1\nline2", "b": 2}


def test_extract_json_object_truncated_mid_value_stays_none() -> None:
    """A generation cut off mid-string-value has no balanced top-level
    block at all — the strict=False second chance must not invent a
    parse for it; this must return ``None`` before AND after the fix."""
    text = '{"logbook": [{"entry_type": "note", "text": "cut off mid'
    assert extract_json_object(text) is None


def test_extract_json_object_stray_bracket_after_string_stays_none() -> None:
    """Prod sample (ref 341220, gr345336 root-cause investigation): a
    big-tier sampling glitch appended a stray unmatched ``]`` immediately
    after a string field's closing quote, before the next key. The braces
    are balanced (so the block extractor finds a full candidate) but the
    syntax itself is broken — not a control-character issue — so
    strict=False must not repair it. Bracket-repair heuristics are
    explicitly out of scope: a legit array elsewhere in the payload must
    never be "fixed" into something it didn't say."""
    text = (
        '{"logbook": [{"entry_type": "note", "text": "hi"}],'
        ' "dossier_text": "closes the thread on this tick."],'
        ' "directions": []}'
    )
    assert extract_json_object(text) is None


def test_extract_json_object_still_parses_clean_json() -> None:
    """Byte-identical to before this fix for ordinary well-formed JSON —
    the strict pass alone must keep winning without ever reaching the
    strict=False fallback."""
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert (
        extract_json_object('prose before {"a": 1} prose after') == {"a": 1}
    )


def test_extract_json_object_no_json_at_all_stays_none() -> None:
    assert extract_json_object("no json in here") is None

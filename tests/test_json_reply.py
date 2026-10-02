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

import logging

import pytest

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
    strict=False must not repair it. The default stays strict; only a caller
    that opts in (``repair_stray_closers=True``, quest tick only) gets the
    narrow repair tested below."""
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
    assert extract_json_object('prose before {"a": 1} prose after') == {"a": 1}


def test_extract_json_object_no_json_at_all_stays_none() -> None:
    assert extract_json_object("no json in here") is None


# --- opt-in stray-closer repair (gr345366 cause B; Reto, review chemistry-1) ---

_STRAY = (
    '{"logbook": [{"entry_type": "note", "text": "hi"}],'
    ' "dossier_text": "closes the thread on this tick."],'
    ' "directions": []}'
)


def test_stray_bracket_repaired_only_when_opted_in(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The same prod shape stays ``None`` by default (above) and parses with
    the repair on, keeping every real array intact; the drop is logged."""
    assert extract_json_object(_STRAY) is None
    with caplog.at_level(logging.WARNING):
        got = extract_json_object(_STRAY, repair_stray_closers=True)
    assert got == {
        "logbook": [{"entry_type": "note", "text": "hi"}],
        "dossier_text": "closes the thread on this tick.",
        "directions": [],
    }
    assert "dropped stray" in caplog.text


def test_stray_repair_inside_prose_and_fence() -> None:
    text = "Here is the tick:\n```json\n" + _STRAY + "\n```\n"
    assert extract_json_object(text, repair_stray_closers=True) is not None


def test_stray_brace_in_array_repaired() -> None:
    text = '{"a": ["x"}], "b": 1}'
    assert extract_json_object(text, repair_stray_closers=True) == {
        "a": ["x"],
        "b": 1,
    }


def test_stray_repair_capped_at_two() -> None:
    two = '{"a": "x"]], "b": 1}'
    three = '{"a": "x"]]], "b": 1}'
    assert extract_json_object(two, repair_stray_closers=True) == {"a": "x", "b": 1}
    assert extract_json_object(three, repair_stray_closers=True) is None


def test_stray_repair_never_rescues_truncation_or_other_errors() -> None:
    for text in (
        '{"logbook": [{"entry_type": "note", "text": "cut off mid',
        '{"a": [1, 2',
        '{"a": "x", "b"]: 1}',
    ):
        assert extract_json_object(text, repair_stray_closers=True) is None

"""The anchor resolve ladder (gr739606): pure, no database."""

from __future__ import annotations

from precis.store._anchors_ops import (
    Anchor,
    ChunkText,
    resolve_code,
    resolve_text,
    text_fingerprint,
)

TEXT = "The yield was 42 % at 300 K."


def _text_anchor(quote: str = "42 %") -> Anchor:
    start = TEXT.index(quote)
    return Anchor(
        1,
        "text",
        quote,
        text_fingerprint(TEXT, "sha-a"),
        ref_id=5,
        chunk_id=10,
        char_start=start,
        char_end=start + len(quote),
    )


def test_text_fingerprint_match_is_ok_at_stored_position() -> None:
    a = _text_anchor()
    r = resolve_text(a, [ChunkText(10, TEXT, "sha-a")])
    assert (r.state, r.method) == ("ok", "fingerprint")
    assert (r.chunk_id, r.char_start, r.char_end) == (10, 14, 18)


def test_text_fingerprint_falls_back_to_text_sha() -> None:
    a = Anchor(
        1, "text", "42", text_fingerprint(TEXT, None),
        chunk_id=10, char_start=14, char_end=16, ref_id=5,
    )  # fmt: skip
    assert resolve_text(a, [ChunkText(10, TEXT, None)]).method == "fingerprint"


def test_text_reingest_moves_to_new_chunk() -> None:
    a = _text_anchor()
    new = "Intro.\n" + TEXT
    r = resolve_text(a, [ChunkText(99, new, "sha-b")])
    assert (r.state, r.method, r.chunk_id) == ("moved", "verbatim", 99)
    assert new[r.char_start : r.char_end] == "42 %"


def test_text_edit_elsewhere_in_chunk_keeps_ok_when_position_same() -> None:
    a = _text_anchor()
    r = resolve_text(a, [ChunkText(10, TEXT + " More.", "sha-b")])
    assert (r.state, r.method) == ("ok", "verbatim")


def test_text_quote_gone_is_stale_without_position() -> None:
    r = resolve_text(_text_anchor(), [ChunkText(10, "Nothing here.", "sha-b")])
    assert (r.state, r.method) == ("stale", "none")
    assert r.chunk_id is None and r.char_start is None


def test_text_ambiguous_quote_is_stale_never_a_guess() -> None:
    chunks = [ChunkText(20, TEXT, "x"), ChunkText(21, TEXT, "y")]
    r = resolve_text(_text_anchor(), chunks)
    assert (r.state, r.method) == ("stale", "ambiguous")


def test_text_own_chunk_is_ambiguous_with_another_hit_after_fingerprint_change() -> (
    None
):
    a = _text_anchor()
    chunks = [ChunkText(10, TEXT + "!", "sha-b"), ChunkText(21, TEXT, "y")]
    assert resolve_text(a, chunks).method == "ambiguous"


_CODE = Anchor(
    2, "code", "def f():\n    return 1", "c0ffee",
    repo="r", symbol_path="m.py::f", line_start=3, line_end=4,
)  # fmt: skip


def test_code_same_commit_is_ok() -> None:
    r = resolve_code(_CODE, "c0ffee", ["x"])
    assert (r.state, r.method, r.line_start, r.line_end) == ("ok", "fingerprint", 3, 4)


def test_code_moved_lines_found_verbatim() -> None:
    lines = ["#", "#", "#", "#", "def f():", "    return 1"]
    r = resolve_code(_CODE, "new", lines)
    assert (r.state, r.method, r.line_start, r.line_end) == ("moved", "verbatim", 5, 6)


def test_code_changed_is_stale_and_missing_file_is_stale() -> None:
    assert resolve_code(_CODE, "new", ["def f():", "    return 2"]).state == "stale"
    assert resolve_code(_CODE, "c0ffee", None).state == "stale"


def test_code_ambiguous_is_stale() -> None:
    lines = ["def f():", "    return 1"] * 2
    assert resolve_code(_CODE, "new", lines).method == "ambiguous"


def test_code_searches_the_repository_and_reports_the_new_path() -> None:
    lines = ["def f():", "    return 1"]
    result = resolve_code(_CODE, "new", {"other.py::f": lines})
    assert (result.state, result.symbol_path, result.line_start) == (
        "moved",
        "other.py::f",
        1,
    )
    assert (
        resolve_code(_CODE, "new", {"other.py::f": lines, "third.py::f": lines}).method
        == "ambiguous"
    )

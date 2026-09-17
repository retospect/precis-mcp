"""Canonical JSON invariance, text<->JSON round-trip, and emit rules."""

from __future__ import annotations

import json

from hexfold.canon import canonical_json, lexmin_word, text_of
from hexfold.text import parse, to_text

_SHEET = "hexfold 0.1\norigin s\ns: sheet(12, 12) + 57@(4,4,A):0\n"


def test_lexmin_word_rotation() -> None:
    # every rotation of the word yields the same canonical word
    syms = "z.a2.z.a2.z2"
    assert lexmin_word(lexmin_word(syms)) == lexmin_word(syms)
    rot = "a.z3.a2.z.a"
    assert lexmin_word(rot) == lexmin_word(syms)


def test_canon_translation_invariant() -> None:
    a = canonical_json(_SHEET)
    b = canonical_json("hexfold 0.1\norigin s\ns: sheet(12, 12) + 57@(7,5,A):0\n")
    assert a == b


def test_canon_rotation_invariant() -> None:
    a = canonical_json(_SHEET)
    b = canonical_json("hexfold 0.1\norigin s\ns: sheet(12, 12) + 57@(4,4,A):3\n")
    assert a == b


def test_canon_mirror_differs() -> None:
    # a chiral defect arrangement vs its mirror: (6,3) vs (3,6) tube
    t1 = canonical_json("hexfold 0.1\norigin t\nt: tube(6, 3, len=2)\n")
    t2 = canonical_json("hexfold 0.1\norigin t\nt: tube(3, 6, len=2)\n")
    assert t1 != t2


def test_canon_deterministic() -> None:
    assert canonical_json(_SHEET) == canonical_json(_SHEET)


def test_canon_json_sorted_no_floats() -> None:
    d = json.loads(canonical_json(_SHEET))

    def walk(x: object) -> None:
        if isinstance(x, dict):
            assert list(x) == sorted(x)
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        else:
            assert not isinstance(x, float)

    walk(d)


def test_emit_uses_unicode_repeat() -> None:
    spec = parse("hexfold 0.1\norigin t\nt: tube(5, 5, len=2) x3\n")
    out = to_text(spec)
    assert "×3" in out
    assert "x3" not in out


def test_round_trip_text_json_text_json() -> None:
    j1 = canonical_json(_SHEET)
    t1 = text_of(j1)
    j2 = canonical_json(t1)
    assert j1 == j2


def test_round_trip_with_0_2_header() -> None:
    sheet_02 = "hexfold 0.2\norigin s\ns: sheet(12, 12) + 57@(4,4,A):0\n"
    j1 = canonical_json(sheet_02)
    assert json.loads(j1)["hexfold"] == "0.2"
    t1 = text_of(j1)
    assert t1.splitlines()[0] == "hexfold 0.2"
    j2 = canonical_json(t1)
    assert j1 == j2


def test_parse_accepts_0_1_header_backward_compat() -> None:
    # 0.1 and 0.2 parse identically in this slice; the emitter always
    # writes 0.2 (0.2 is a format bump, not a new dialect to preserve).
    spec = parse(_SHEET)
    assert spec.version == "0.2"


def test_parse_rejects_unsupported_version() -> None:
    import pytest

    from hexfold.report import ParseError

    with pytest.raises(ParseError):
        parse("hexfold 0.3\norigin s\ns: sheet(12, 12)\n")


def test_parse_ascii_x_accepted() -> None:
    a = parse("hexfold 0.1\norigin t\nt: tube(5, 5, len=2) x3\n")
    b = parse("hexfold 0.1\norigin t\nt: tube(5, 5, len=2) ×3\n")
    assert a.instances[0].repeat == b.instances[0].repeat == 3


def test_parse_error_has_span() -> None:
    import pytest

    from hexfold.report import ParseError

    with pytest.raises(ParseError) as ei:
        parse("hexfold 0.1\norigin s\nthis is not valid\n")
    assert ei.value.span is not None and ei.value.span[0] == 3

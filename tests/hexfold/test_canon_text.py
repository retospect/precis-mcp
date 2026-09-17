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


def test_run_length_never_compresses_digit_symbols() -> None:
    # "55" -> "52" would read back as the single symbol 52 (expand_word
    # takes \d+ as one symbol); two equal port words then serialised
    # differently and the canonical JSON keyed one structure two ways
    from hexfold.defects import expand_word, run_length

    assert run_length("55") == "5.5"
    assert run_length("zz55aa") == "z2.5.5.a2"
    word = "2z55z5"
    assert list(expand_word(run_length(word))) == list(word)
    assert lexmin_word(run_length(word)) == lexmin_word(run_length("z5" + "2z55"))


def test_canon_bounded_sheet_defect_offset_is_structural() -> None:
    # 0.1 asserted translation/rotation *invariance* here -- it held only
    # because both authorings were re-framed to the same clipped corner
    # glyph.  On a bounded sheet the defect's offset from the rim is part
    # of the structure (the port words differ), so a frame that changes it
    # is no symmetry and the two must NOT collide (SPEC 14.2 note).
    a = canonical_json(_SHEET)
    b = canonical_json("hexfold 0.1\norigin s\ns: sheet(12, 12) + 57@(7,5,A):0\n")
    c = canonical_json("hexfold 0.1\norigin s\ns: sheet(12, 12) + 57@(4,4,A):3\n")
    assert a != b and a != c
    d = json.loads(a)
    assert d["instances"]["s"]["defects"][0]["site"] == "(4,4,A)"


def test_canon_rejects_frame_that_does_not_build() -> None:
    # C60 "sites" are labels, not a lattice: translating a pentagon hole
    # to (0,0,·) lands on a label that may not exist, so the frame does
    # not build and the authored site is kept (the cage's icosahedral
    # symmetry is roadmap, not a lattice translation)
    from hexfold.fullerene import C60Data

    d = C60Data()
    sites = d.sites()
    other = sites[d.pentagons[3][0]]
    assert str(other) != "(0,0,A)"
    b = canonical_json(f"hexfold 0.2\norigin c\nc: fullerene(C60) - pentagon@{other}\n")
    assert json.loads(b)["instances"]["c"]["holes"][0]["site"] == str(other)
    # equivalent holes build the same port word regardless of site
    a = canonical_json("hexfold 0.2\norigin c\nc: fullerene(C60) - pentagon@(0,0,A)\n")
    assert (
        json.loads(a)["ports"]["hole"]["word"] == json.loads(b)["ports"]["hole"]["word"]
    )


def test_canon_mirror_differs() -> None:
    # a chiral defect arrangement vs its mirror: (6,3) vs (3,6) tube
    t1 = canonical_json("hexfold 0.1\norigin t\nt: tube(6, 3, len=2)\n")
    t2 = canonical_json("hexfold 0.1\norigin t\nt: tube(3, 6, len=2)\n")
    assert t1 != t2


def test_canon_deterministic() -> None:
    assert canonical_json(_SHEET) == canonical_json(_SHEET)


def test_canon_tube_keeps_authored_frame() -> None:
    # a bounded tube's rims make the axial position of a hole structural:
    # 0.1 anchored every defect at (0,0) and dragged a flank hole onto the
    # `in` rim (found by the se dogfood, capped_tube_da_neck.hx)
    flank = "hexfold 0.2\norigin t\nt: tube(5,5, len=14) - hex(0)@(7,0,A):0\n"
    rim = "hexfold 0.2\norigin t\nt: tube(5,5, len=14) - hex(0)@(0,0,A):0\n"
    assert canonical_json(flank) != canonical_json(rim)
    d = json.loads(canonical_json(flank))
    assert d["instances"]["t"]["holes"][0]["site"] == "(7,0,A)"


_BUD_ON_SHEET = (
    "hexfold 0.2\norigin h\nh: sheet(14,14) + 57@(4,4,A):0\n"
    "b: fullerene(C60)\nb @ h/(9,9,A):0 [2+2]\n"
)
_BUD_ON_SHEET_SHIFTED = (
    "hexfold 0.2\norigin h\nh: sheet(14,14) + 57@(6,5,A):0\n"
    "b: fullerene(C60)\nb @ h/(11,10,A):0 [2+2]\n"
)


def test_canon_keeps_bud_on_sheet_structure() -> None:
    # a bounded sheet with a site-addressed bud: no frame survives the
    # structure check, so the canonical spec IS the authored structure and
    # a built net canonicalises exactly like its text
    from hexfold.build import build
    from hexfold.canon import canonicalise

    authored = build(_BUD_ON_SHEET, strict=True)
    canon = build(canonicalise(parse(_BUD_ON_SHEET)), strict=True)
    assert len(canon.atoms) == len(authored.atoms)
    assert len(canon.bonds) == len(authored.bonds)
    assert {n for n, _ in canon.ports} == {n for n, _ in authored.ports}
    assert canonical_json(authored) == canonical_json(_BUD_ON_SHEET)
    assert canonical_json(_BUD_ON_SHEET) != canonical_json(_BUD_ON_SHEET_SHIFTED)


def test_reframe_connect_moves_every_site_reference() -> None:
    # the frame must re-express `h/(u,v,s)` endpoints on the origin
    # instance -- in the connect itself and in a menu's expansion record
    # (connect lines and the bare hole-site list, non-site markers kept)
    from hexfold.canon import _reframe_connect, _site_ref_re
    from hexfold.text import Connect

    c = Connect(
        src="b",
        dst="h/(9,9,A):0",
        verb="menu",
        menu="DA-neck(3)",
        expanded={
            "menu": "DA-neck(3)",
            "connects": [
                "b/(0,0,A) --bond--> h/(9,9,A)",
                "b.hole --fuse k=0--> neck.in",
            ],
            "holes": {"b": ["(0,0,A)"], "h": ["path3", "(9,9,B)"]},
        },
    )
    moved = _reframe_connect(c, _site_ref_re("h"), "h", (4, 4), 0)
    assert moved.dst == "h/(5,5,A):0"
    assert moved.src == "b"
    assert moved.expanded is not None
    assert moved.expanded["connects"] == [
        "b/(0,0,A) --bond--> h/(5,5,A)",
        "b.hole --fuse k=0--> neck.in",
    ]
    assert moved.expanded["holes"] == {"b": ["(0,0,A)"], "h": ["path3", "(5,5,B)"]}


def test_canon_net_equals_text_with_generated_hole_on_origin() -> None:
    # a menu that cuts a hole into the origin instance (DA-neck's path3
    # host opening) exists on the built net but not on the unexpanded
    # text; anchoring on authored defects only keeps the two byte-equal
    from hexfold.build import build

    text = (
        "hexfold 0.2\norigin h\nh: sheet(16,16) + 57@(3,3,A):0\n"
        "b: fullerene(C60)\nb @ h/(9,9,A):0 [DA-neck(3)]\n"
    )
    net = build(text, strict=True)
    assert canonical_json(net) == canonical_json(text)
    d = json.loads(canonical_json(net))
    holes = d["instances"]["h"]["holes"]
    assert [h.get("source") for h in holes] == ["DA-neck(3)"]


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

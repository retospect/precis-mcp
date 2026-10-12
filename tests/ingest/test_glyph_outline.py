"""Glyph-outline fingerprint recovery (gr228652, backlog §Recovery).

The fonts here are synthetic, built with fontTools: a glyph *named* ``m``
or ``C22`` but *drawn* as a micro sign stands in for the pa494 (lying
``/ToUnicode``) and pa47024 (no ``/ToUnicode``, C0 residue) scars. The
publisher PDFs themselves are not fixtures.
"""

from __future__ import annotations

import io
from typing import Any

import pytest

from precis.ingest import glyph_outline as go
from precis.ingest.glyph_health import classify_font

# A micro-sign-like contour (stem with a descender + a bowl) and a plain
# square; coordinates in a 1000-unit em.
_MU: list[tuple[float, float]] = [
    (80, -200),
    (160, -200),
    (160, 120),
    (300, 40),
    (420, 120),
    (420, 500),
    (340, 500),
    (340, 180),
    (240, 120),
    (160, 200),
    (160, 500),
    (80, 500),
]
_SQUARE: list[tuple[float, float]] = [(100, 0), (500, 0), (500, 400), (100, 400)]
_TRIANGLE: list[tuple[float, float]] = [(0, 0), (600, 0), (300, 520)]


def _draw(
    pen: Any,
    pts: list[tuple[float, float]],
    scale: float = 1.0,
    dx: float = 0,
    dy: float = 0,
) -> None:
    first, *rest = pts
    pen.moveTo((first[0] * scale + dx, first[1] * scale + dy))
    for x, y in rest:
        pen.lineTo((x * scale + dx, y * scale + dy))
    pen.closePath()


def _record(
    pts: list[tuple[float, float]], scale: float = 1.0, dx: float = 0, dy: float = 0
) -> list[Any]:
    from fontTools.pens.recordingPen import RecordingPen

    pen = RecordingPen()
    _draw(pen, pts, scale, dx, dy)
    return pen.value


def _cff_font(
    glyphs: dict[str, list[tuple[float, float]]], upem: int = 1000, dx: float = 0
) -> bytes:
    """A bare CFF program (what a PDF ``/FontFile3 /Type1C`` holds)."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.t2CharStringPen import T2CharStringPen

    scale = upem / 1000.0
    order = [".notdef", *glyphs]
    fb = FontBuilder(upem, isTTF=False)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap({})
    charstrings = {}
    for name in order:
        pen = T2CharStringPen(500, None)
        if name in glyphs:
            _draw(pen, glyphs[name], scale, dx)
        charstrings[name] = pen.getCharString()
    fb.setupCFF("TestAdvP", {"FullName": "TestAdvP"}, charstrings, {})
    fb.setupHorizontalMetrics({n: (500, 0) for n in order})
    fb.setupHorizontalHeader()
    fb.setupNameTable({"familyName": "TestAdvP", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    out = io.BytesIO()
    fb.font["CFF "].cff.compile(out, fb.font)
    return out.getvalue()


def _ttf_font(
    glyphs: dict[str, list[tuple[float, float]]], cmap: dict[int, str], upem: int = 2048
) -> bytes:
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    scale = upem / 1000.0
    order = [".notdef", *glyphs]
    fb = FontBuilder(upem, isTTF=True)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap(cmap)
    built = {}
    for name in order:
        pen = TTGlyphPen(None)
        if name in glyphs:
            _draw(pen, glyphs[name], scale)
        built[name] = pen.glyph()
    fb.setupGlyf(built)
    fb.setupHorizontalMetrics({n: (500, 0) for n in order})
    fb.setupHorizontalHeader()
    fb.setupNameTable({"familyName": "TestTT", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    out = io.BytesIO()
    fb.save(out)
    return out.getvalue()


def _mu_fp() -> str:
    fp = go.fingerprint_path(_record(_MU), 1.0)
    assert fp is not None
    return fp


# ---------------------------------------------------------------------------
# fingerprint_path
# ---------------------------------------------------------------------------


def test_fingerprint_ignores_translation_and_em_size() -> None:
    base = _mu_fp()
    assert go.fingerprint_path(_record(_MU, dx=37, dy=-91), 1.0) == base
    # Same outline in a 2048-unit em, scaled back to 1000.
    assert go.fingerprint_path(_record(_MU, scale=2.048), 1000 / 2048) == base
    assert base.startswith(f"{go.FINGERPRINT_SCHEME}:")


def test_fingerprint_separates_shapes_and_skips_empty() -> None:
    assert go.fingerprint_path(_record(_SQUARE), 1.0) != _mu_fp()
    assert go.fingerprint_path([], 1.0) is None


# ---------------------------------------------------------------------------
# Font programs
# ---------------------------------------------------------------------------


def test_cff_outline_is_independent_of_glyph_name_and_upem() -> None:
    prog = go.open_font_program(_cff_font({"m": _MU, "C22": _MU, "a": _SQUARE}), "cff")
    assert prog.fingerprint("m") == prog.fingerprint("C22") == _mu_fp()
    assert prog.fingerprint("a") != _mu_fp()
    # Standard encoding: code 0x6d is glyph 'm'.
    assert prog.encoding[0x6D] == "m"

    big = go.open_font_program(_cff_font({"m": _MU}, upem=2048, dx=55), "cff")
    assert big.fingerprint("m") == _mu_fp()


def test_truetype_symbol_cmap_maps_codes() -> None:
    buf = _ttf_font({"g7": _MU}, {0xF002: "g7"})
    prog = go.open_font_program(buf, "ttf")
    assert prog.encoding[2] == "g7"
    assert prog.fingerprint("g7") == _mu_fp()


def test_unsupported_program_raises() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        go.open_font_program(b"", "n/a")


def _type1_font(glyphs: dict[str, list[tuple[float, float]]]) -> bytes:
    """A Type1 program as a PDF ``/FontFile`` holds it (cleartext + binary eexec)."""
    from fontTools import t1Lib
    from fontTools.encodings.StandardEncoding import StandardEncoding
    from fontTools.misc.psCharStrings import T1CharString

    def charstring(pts: list[tuple[float, float]]) -> T1CharString:
        prog: list[Any] = [0, 500, "hsbw"]
        x0, y0 = 0.0, 0.0
        for i, (x, y) in enumerate(pts):
            prog += [int(x - x0), int(y - y0), "rmoveto" if i == 0 else "rlineto"]
            x0, y0 = x, y
        prog += ["closepath", "endchar"]
        cs = T1CharString(program=prog)
        cs.compile()
        return cs

    t1 = t1Lib.T1Font.__new__(t1Lib.T1Font)
    t1.encoding = "ascii"
    t1.font = {
        "FontName": "TestT1",
        "FontType": 1,
        "PaintType": 0,
        "FontMatrix": [0.001, 0, 0, 0.001, 0, 0],
        "FontBBox": [0, -250, 1000, 750],
        "Encoding": StandardEncoding,
        "Private": {
            "RD": ("string", "currentfile", "exch", "readstring", "pop"),
            "ND": ("def",),
            "NP": ("put",),
            "lenIV": 4,
            "Subrs": [],
            "BlueValues": [],
        },
        "CharStrings": {
            ".notdef": charstring([(0, 0), (1, 0), (1, 1)]),
            **{n: charstring(p) for n, p in glyphs.items()},
        },
    }
    data: bytes = t1.createData()
    return data


def _pfb_wrap(data: bytes) -> bytes:
    split = data.index(b"eexec") + len(b"eexec ")
    out = b""
    for kind, seg in ((1, data[:split]), (2, data[split:])):
        out += bytes([0x80, kind]) + len(seg).to_bytes(4, "little") + seg
    return out + b"\x80\x03"


def test_type1_outline_matches_cff_and_survives_pfb_wrapping() -> None:
    data = _type1_font({"m": _MU})
    assert go.open_font_program(data, "pfa").fingerprint("m") == _mu_fp()
    assert go.open_font_program(_pfb_wrap(data), "pfb").fingerprint("m") == _mu_fp()


def test_open_contour_and_closing_lineto_fingerprint_as_closed() -> None:
    closed = _record(_MU)
    redundant = [*closed[:-1], ("lineTo", (_MU[0],)), ("closePath", ())]
    open_path = [*closed[:-1], ("endPath", ())]
    assert go.fingerprint_path(redundant, 1.0) == _mu_fp()
    assert go.fingerprint_path(open_path, 1.0) == _mu_fp()


def test_pdf_base_encoding_overrides_builtin_encoding() -> None:
    """AdvP0004 shape: WinAnsi declared, no ToUnicode. 0xB1 is 'plusminus'
    in WinAnsi but 'endash' in the program's StandardEncoding."""
    prog = go.open_font_program(_cff_font({"plusminus": _MU}), "cff")
    font_info = {"basefont": "AdvP0004", "symbolic": True, "has_encoding": True}
    table = {_mu_fp(): {"char": "≤"}}
    assert go.resolve_font(prog, font_info, {"symbolic_no_tounicode"}, table) == (
        [],
        [],
    )
    resolved, _ = go.resolve_font(
        prog,
        {**font_info, "base_encoding": "WinAnsiEncoding"},
        {"symbolic_no_tounicode"},
        table,
    )
    assert [(r["code"], r["glyph"], r["char"]) for r in resolved] == [
        (0xB1, "plusminus", "≤")
    ]


def test_truetype_falls_back_to_cmap_when_pdf_name_is_absent() -> None:
    prog = go.open_font_program(_ttf_font({"g7": _MU}, {0xF002: "g7"}), "ttf")
    font_info = {"basefont": "AdvTT", "differences": {2: "C22"}}
    resolved, _ = go.resolve_font(prog, font_info, {"b"}, {_mu_fp(): {"char": "μ"}})
    assert [(r["code"], r["glyph"]) for r in resolved] == [(2, "g7")]


def test_composite_fonts_are_skipped() -> None:
    class _Doc:
        def extract_font(self, xref: int) -> Any:
            raise AssertionError("composite font must not be opened")

    fonts = [
        {
            "xref": 9,
            "subtype": "Type0",
            "basefont": "AdvP4C4E51",
            "has_tounicode": False,
        }
    ]
    rec = go.resolve_document(_Doc(), fonts, classify_font)
    assert rec["resolved"] == [] and rec["misses"] == [] and "errors" not in rec


# ---------------------------------------------------------------------------
# resolve_font: the two scar shapes and a miss
# ---------------------------------------------------------------------------


def test_mode_a_lying_tounicode_resolves_mu() -> None:
    """pa494 shape: glyph named 'm', drawn as μ, ToUnicode <6d> -> 'm'."""
    prog = go.open_font_program(_cff_font({"m": _MU}), "cff")
    font_info = {
        "basefont": "KKLGAD+AdvP7DA6",
        "has_tounicode": True,
        "has_encoding": True,
        "differences": {0x6D: "m"},
        "tounicode": {0x6D: "m"},
    }
    modes = classify_font(font_info)
    assert "a" in modes
    table = {_mu_fp(): {"char": "μ"}}
    resolved, misses = go.resolve_font(prog, font_info, modes, table)
    assert resolved == [
        {
            "font": "KKLGAD+AdvP7DA6",
            "code": 0x6D,
            "glyph": "m",
            "emits": "m",
            "char": "μ",
        }
    ]
    assert misses == []


def test_mode_b_positional_glyph_resolves_c0_residue_and_records_miss() -> None:
    """pa47024 shape: no ToUnicode, /Differences [2 /C22 3 /C23]."""
    prog = go.open_font_program(_cff_font({"C22": _MU, "C23": _TRIANGLE}), "cff")
    font_info = {
        "basefont": "IBDHKG+AdvP4C4E51",
        "has_tounicode": False,
        "has_encoding": True,
        "differences": {2: "C22", 3: "C23"},
    }
    modes = classify_font(font_info)
    assert "b" in modes
    table = {_mu_fp(): {"char": "μ"}}
    resolved, misses = go.resolve_font(prog, font_info, modes, table)
    assert [(r["code"], r["emits"], r["char"]) for r in resolved] == [(2, "\x02", "μ")]
    assert len(misses) == 1
    miss = misses[0]
    assert (miss["code"], miss["glyph"], miss["emits"]) == (3, "C23", "\x03")
    assert miss["outline"] == go.fingerprint_path(_record(_TRIANGLE), 1.0)
    assert "char" not in miss


def test_mode_a_checks_only_echoing_codes() -> None:
    font_info = {
        "basefont": "AdvP7DA6",
        "differences": {0x6D: "m", 0x41: "Alpha"},
        "tounicode": {0x6D: "m", 0x41: "Α"},
    }
    assert go.suspect_codes(font_info, {"a"}, {}) == {0x6D: "m"}
    assert go.suspect_codes(font_info, {"adv_family"}, {}) == {}


def test_packaged_table_loads() -> None:
    table = go.outline_table()
    assert isinstance(table, dict)
    for fp, entry in table.items():
        assert fp.startswith(f"{go.FINGERPRINT_SCHEME}:")
        assert entry["char"] and entry["font"] and entry["paper"]


# ---------------------------------------------------------------------------
# End to end through a PDF (needs PyMuPDF)
# ---------------------------------------------------------------------------


def _pdf_with_type1c(
    cff: bytes, *, differences: str, tounicode: bytes | None, text: bytes
) -> bytes:
    """A one-page PDF whose only font is a simple Type1 font with an
    embedded ``/FontFile3 /Type1C`` program."""
    objs: list[bytes] = []

    def stream(dict_body: bytes, data: bytes) -> bytes:
        return (
            b"<< "
            + dict_body
            + b" /Length %d >>\nstream\n" % len(data)
            + data
            + b"\nendstream"
        )

    content = b"BT /F1 24 Tf 72 700 Td (" + text + b") Tj ET"
    tou_ref = b" /ToUnicode 7 0 R" if tounicode is not None else b""
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objs.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
    )
    objs.append(stream(b"", content))
    objs.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /"
        + b"ABCDEF+AdvPTest"
        + b" /FirstChar 0 /LastChar 255 /Encoding << /Type /Encoding /Differences ["
        + differences.encode("ascii")
        + b"] >> /FontDescriptor 6 0 R"
        + tou_ref
        + b" >>"
    )
    objs.append(
        b"<< /Type /FontDescriptor /FontName /ABCDEF+AdvPTest /Flags 4 /FontBBox [0 -250 1000 750]"
        b" /ItalicAngle 0 /Ascent 750 /Descent -250 /CapHeight 700 /StemV 80 /FontFile3 8 0 R >>"
    )
    objs.append(stream(b"", tounicode or b""))
    objs.append(stream(b"/Subtype /Type1C", cff))

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objs) + 1, xref)
    )
    return out.getvalue()


_TOU_M = (
    b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
    b"1 begincodespacerange <00> <FF> endcodespacerange\n"
    b"1 beginbfchar <6d> <006D> endbfchar\n"
    b"endcmap CMapName currentdict /CMap defineresource pop end end"
)


@pytest.mark.parametrize(
    ("differences", "tounicode", "text", "code", "emits", "mode"),
    [
        ("109 /m", _TOU_M, b"V/m", 0x6D, "m", "a"),
        ("2 /C22", None, b"6 \\002m", 2, "\x02", "b"),
    ],
)
def test_analyze_pdf_records_recovery(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    differences: str,
    tounicode: bytes | None,
    text: bytes,
    code: int,
    emits: str,
    mode: str,
) -> None:
    pytest.importorskip("fitz")
    from precis.ingest import glyph_health

    glyph = differences.split("/")[1]
    pdf = tmp_path / "scar.pdf"
    pdf.write_bytes(
        _pdf_with_type1c(
            _cff_font({glyph: _MU}),
            differences=differences,
            tounicode=tounicode,
            text=text,
        )
    )
    monkeypatch.setattr(go, "outline_table", lambda: {_mu_fp(): {"char": "μ"}})

    record = glyph_health.analyze_pdf(pdf)
    assert record["suspected"] is True
    assert mode in record["modes"]
    resolved = record["recovery"]["resolved"]
    assert [(r["code"], r["emits"], r["char"]) for r in resolved] == [
        (code, emits, "μ")
    ]
    assert record["recovery"]["misses"] == []


def test_analyze_pdf_unknown_outline_is_a_miss(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fitz")
    from precis.ingest import glyph_health

    pdf = tmp_path / "unknown.pdf"
    pdf.write_bytes(
        _pdf_with_type1c(
            _cff_font({"C22": _TRIANGLE}),
            differences="2 /C22",
            tounicode=None,
            text=b"\\002",
        )
    )
    monkeypatch.setattr(go, "outline_table", lambda: {_mu_fp(): {"char": "μ"}})

    rec = glyph_health.analyze_pdf(pdf)["recovery"]
    assert rec["resolved"] == []
    assert [(m["code"], m["glyph"]) for m in rec["misses"]] == [(2, "C22")]
    assert rec["misses"][0]["outline"].startswith(f"{go.FINGERPRINT_SCHEME}:")

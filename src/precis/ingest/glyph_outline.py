"""Glyph-outline fingerprinting: recover the true character of a flagged glyph.

Recovery half of the Greek-strip defect (gr228652; plan in
``docs/backlog/ingest-strips-greek-glyphs.md`` §Recovery, ruled 2026-10-11).
:mod:`precis.ingest.glyph_health` detects a font whose encoding lies (mode
"a": ``/ToUnicode`` echoes a glyph named ``m`` that is drawn as ``μ``;
mode "b": no ``/ToUnicode``, positional names, C0 residue). The text layer
cannot say what those glyphs are, but the embedded font program still holds
their outlines, and an outline does not lie.

So for each suspect (font, code) we draw the glyph from the embedded font
program, normalise the outline and hash it:

* hints are dropped (a pen only sees the drawn path),
* the path is translated so its bounding box starts at the origin,
* coordinates are scaled to a 1000-unit em and rounded,
* glyph names and subset numbering play no part — only the path does,
* an open contour (``endPath``) counts as closed, and a closing ``lineTo``
  back to the contour's start is dropped.

The hash is stable per outline *format*: a TrueType ``qCurveTo`` never
equals a CFF/Type1 ``curveTo``, and a non-1000 em can round a coordinate
by one unit. One character may therefore need several table entries; the
table is keyed by hash, so that costs nothing.

The hash is looked up in ``precis/data/glyph_outlines.json``, a table of
known outlines seeded from the two confirmed scars (pa494, pa47024). A hit
gives the true codepoint plus the character the extractor emitted instead,
which is everything a re-extraction needs. A miss is recorded with its
hash so the table can grow from real documents. OCR is ruled out (see the
backlog); a model pass over rendered regions is the fallback, built only
once the share of misses is measured.

Simple (single-byte) fonts only: Type1 (``/FontFile``), bare CFF
(``/FontFile3``) and TrueType (``/FontFile2``). Composite ``Type0`` fonts
are skipped; none of the confirmed scars uses one.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
from functools import lru_cache
from importlib import resources
from typing import Any

log = logging.getLogger(__name__)

#: Version tag baked into every fingerprint. Bump it when the normalisation
#: changes, so stale table entries can never match a new-scheme hash.
FINGERPRINT_SCHEME = "o1"

#: Bounds on the entries kept per document, so a big symbol font cannot
#: bloat ``meta``.
MAX_MISSES = 32
MAX_RESOLVED = 64
MAX_ERRORS = 8

#: PDF font subtypes whose codes are single bytes. ``Type1`` covers bare CFF
#: (``/FontFile3 /Type1C``) too. Composite (``Type0``/CID) and ``Type3``
#: fonts are skipped: their codes do not index the program's encoding.
_SIMPLE_SUBTYPES = frozenset({"Type1", "MMType1", "TrueType"})

#: Glyphs that draw nothing worth matching.
_SKIP_GLYPHS = frozenset({".notdef", "space", "nbspace", ".null", "nonmarkingreturn"})


# ---------------------------------------------------------------------------
# Fingerprint (pure over a recorded pen path)
# ---------------------------------------------------------------------------


def fingerprint_path(
    commands: list[tuple[str, tuple[Any, ...]]], scale: float
) -> str | None:
    """Hash a recorded pen path (``RecordingPen.value``) after normalising it.

    ``scale`` maps font units to a 1000-unit em. Returns ``None`` for an
    empty outline (a space-like glyph has nothing to fingerprint).
    """
    commands = _canonical_path(commands)
    points = [pt for _op, args in commands for pt in args if pt is not None]
    if not points:
        return None
    min_x = min(p[0] for p in points)
    min_y = min(p[1] for p in points)
    parts: list[str] = []
    for op, args in commands:
        coords = ",".join(
            f"{round((p[0] - min_x) * scale)} {round((p[1] - min_y) * scale)}"
            for p in args
            if p is not None
        )
        parts.append(f"{op}({coords})")
    digest = hashlib.sha256(";".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{FINGERPRINT_SCHEME}:{digest}"


def _canonical_path(
    commands: list[tuple[str, tuple[Any, ...]]],
) -> list[tuple[str, tuple[Any, ...]]]:
    """Fold ``endPath`` into ``closePath`` and drop a closing ``lineTo`` that
    returns to the contour's start (closing is implied either way)."""
    out: list[tuple[str, tuple[Any, ...]]] = []
    start: Any = None
    for op, args in commands:
        if op == "moveTo":
            start = args[0] if args else None
        if op in {"closePath", "endPath"}:
            if (
                out
                and out[-1][0] == "lineTo"
                and out[-1][1]
                and out[-1][1][-1] == start
            ):
                out.pop()
            out.append(("closePath", ()))
            continue
        out.append((op, tuple(args)))
    return out


# ---------------------------------------------------------------------------
# Font programs: code -> glyph name, glyph name -> fingerprint
# ---------------------------------------------------------------------------


class FontProgram:
    """One embedded font program, opened with fontTools.

    ``encoding`` is the program's built-in code -> glyph-name vector (what
    the PDF uses when it declares no ``/Differences``); ``fingerprint``
    draws and hashes one glyph by name.
    """

    def __init__(self, glyphs: Any, scale: float, encoding: dict[int, str]) -> None:
        self._glyphs = glyphs
        self.scale = scale
        self.encoding = encoding

    def has_glyph(self, name: str) -> bool:
        return name in self._glyphs

    def fingerprint(self, name: str) -> str | None:
        from fontTools.pens.recordingPen import RecordingPen

        pen = RecordingPen()
        self._glyphs[name].draw(pen)
        return fingerprint_path(pen.value, self.scale)


def pdf_base_encoding(name: str | None) -> dict[int, str]:
    """The code -> glyph-name vector of a PDF base encoding name
    (``WinAnsiEncoding``, ``MacRomanEncoding``, ``StandardEncoding``);
    ``{}`` for anything else."""
    if name == "StandardEncoding":
        return _encoding_vector("StandardEncoding")
    if name == "MacRomanEncoding":
        from fontTools.encodings.MacRoman import MacRoman

        return _encoding_vector(MacRoman)
    if name == "WinAnsiEncoding":
        from fontTools.agl import UV2AGL

        vec: dict[int, str] = {}
        for code in range(32, 256):
            try:
                ch = bytes([code]).decode("cp1252")
            except UnicodeDecodeError:
                continue
            glyph = UV2AGL.get(ord(ch))
            if glyph:
                vec[code] = glyph
        return vec
    return {}


def _encoding_vector(enc: Any) -> dict[int, str]:
    """Normalise a fontTools encoding (list, dict or the name of a standard one)."""
    if enc == "StandardEncoding" or enc is None:
        from fontTools.encodings.StandardEncoding import StandardEncoding

        enc = StandardEncoding
    if isinstance(enc, dict):
        return {int(k): str(v) for k, v in enc.items() if v and v != ".notdef"}
    if isinstance(enc, list | tuple):
        return {i: str(n) for i, n in enumerate(enc) if n and n != ".notdef"}
    return {}


def _open_cff(buf: bytes) -> FontProgram:
    from fontTools.cffLib import CFFFontSet

    fs = CFFFontSet()
    fs.decompile(io.BytesIO(buf), None)
    top = fs[fs.fontNames[0]]
    matrix = getattr(top, "FontMatrix", None) or [0.001]
    enc = getattr(top, "Encoding", "StandardEncoding")
    return FontProgram(top.CharStrings, matrix[0] * 1000.0, _encoding_vector(enc))


def _open_type1(buf: bytes) -> FontProgram:
    from fontTools import t1Lib

    # T1Font only reads from a path, so do what its readers do on bytes: an
    # embedded /FontFile is cleartext + eexec (binary, or hex in a PFA),
    # sometimes still PFB-segmented.
    if buf[:1] == b"\x80":
        buf = _strip_pfb(buf)
    chunks = []
    for encrypted, chunk in t1Lib.findEncryptedChunks(buf):
        if encrypted and t1Lib.isHex(chunk[:4]):
            chunk = t1Lib.deHexString(chunk)
        chunks.append(chunk)
    t1 = t1Lib.T1Font.__new__(t1Lib.T1Font)
    t1.data = b"".join(chunks)
    t1.encoding = "ascii"
    t1.parse()
    matrix = t1.font.get("FontMatrix") or [0.001]
    return FontProgram(
        t1.getGlyphSet(),
        float(matrix[0]) * 1000.0,
        _encoding_vector(t1.font.get("Encoding")),
    )


def _strip_pfb(buf: bytes) -> bytes:
    """Concatenate the data segments of a PFB-wrapped Type1 program."""
    out = []
    pos = 0
    while pos + 2 <= len(buf) and buf[pos] == 0x80 and buf[pos + 1] in (1, 2):
        length = int.from_bytes(buf[pos + 2 : pos + 6], "little")
        out.append(buf[pos + 6 : pos + 6 + length])
        pos += 6 + length
    return b"".join(out)


def _open_truetype(buf: bytes) -> FontProgram:
    from fontTools.ttLib import TTFont

    tt = TTFont(io.BytesIO(buf))
    upem = tt["head"].unitsPerEm or 1000
    enc: dict[int, str] = {}
    if "cmap" in tt:
        for table in tt["cmap"].tables:
            # Symbol fonts put a simple font's codes at U+F000+code (the
            # (3,0) convention; some subsetters write it into a (3,1) table).
            for cp, name in table.cmap.items():
                code = cp - 0xF000 if 0xF000 <= cp <= 0xF0FF else cp
                if 0 <= code < 256:
                    enc.setdefault(code, name)
    return FontProgram(tt.getGlyphSet(), 1000.0 / upem, enc)


def open_font_program(buf: bytes, ext: str) -> FontProgram:
    """Open an embedded font program by PyMuPDF's ``extract_font`` extension."""
    ext = (ext or "").lower()
    if ext == "cff":
        return _open_cff(buf)
    if ext in {"pfa", "pfb"}:
        return _open_type1(buf)
    if ext in {"ttf", "otf"}:
        if buf[:4] == b"OTTO":
            from fontTools.ttLib import TTFont

            tt = TTFont(io.BytesIO(buf))
            return _open_cff(tt["CFF "].compile(tt))
        return _open_truetype(buf)
    raise ValueError(f"unsupported font program: {ext or 'none'}")


# ---------------------------------------------------------------------------
# Known-outline table
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def outline_table() -> dict[str, dict[str, Any]]:
    """The packaged known-outline table: fingerprint -> {char, font, glyph, paper}."""
    raw = json.loads(
        resources.files("precis.data")
        .joinpath("glyph_outlines.json")
        .read_text(encoding="utf-8")
    )
    return {
        k: dict(v)
        for k, v in raw.get("outlines", {}).items()
        if isinstance(v, dict) and v.get("char")
    }


# ---------------------------------------------------------------------------
# Resolution of the suspect codes in one font
# ---------------------------------------------------------------------------


def suspect_codes(
    font_info: dict[str, Any], modes: set[str], encoding: dict[int, str]
) -> dict[int, str]:
    """The (code -> glyph name) pairs worth fingerprinting in one flagged font.

    Mode "a" lies only on the codes whose ``/ToUnicode`` echoes a one-letter
    glyph name, so only those are checked; checking every honest letter of
    the font would fill the record with misses. For mode "b" and a symbolic
    font without ``/ToUnicode`` nothing in the font can be trusted, so every
    encoded code is checked (these are small symbol subsets).

    The code -> name map follows the PDF: the font's declared base encoding
    (``base_encoding``) replaces the program's built-in ``encoding`` when
    present, and ``/Differences`` overlays either.
    """
    names = pdf_base_encoding(font_info.get("base_encoding")) or dict(encoding)
    names.update(font_info.get("differences") or {})
    if "b" in modes or "symbolic_no_tounicode" in modes:
        return names
    if "a" in modes:
        tou = font_info.get("tounicode") or {}
        return {
            code: name
            for code, name in names.items()
            if len(name) == 1 and name.isalpha() and tou.get(code) == name
        }
    return {}


def emitted_char(font_info: dict[str, Any], code: int) -> str:
    """What the extractor emits for ``code``: the ``/ToUnicode`` target, else
    the raw code (mode "b" C0 residue, or a WinAnsi letter like ``G``)."""
    tou = font_info.get("tounicode") or {}
    return str(tou.get(code, chr(code)))


def resolve_font(
    program: FontProgram,
    font_info: dict[str, Any],
    modes: set[str],
    table: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fingerprint the suspect codes of one font; return ``(resolved, misses)``.

    A resolved entry carries ``font``, ``code``, ``glyph``, ``emits`` (what
    the extractor produced) and ``char`` (the true character). A miss
    carries the ``outline`` hash instead of ``char``.
    """
    table = outline_table() if table is None else table
    basefont = str(font_info.get("basefont") or "?")
    resolved: list[dict[str, Any]] = []
    misses: list[dict[str, Any]] = []
    for code, name in sorted(suspect_codes(font_info, modes, program.encoding).items()):
        if not program.has_glyph(name):
            # A subset TrueType program rarely keeps the PDF's glyph names;
            # fall back to the program's own entry for the code.
            name = program.encoding.get(code, name)
        if name in _SKIP_GLYPHS or not program.has_glyph(name):
            continue
        try:
            fp = program.fingerprint(name)
        except Exception as exc:  # one broken charstring must not sink the font
            log.debug("glyph_outline: %s/%s draw failed: %s", basefont, name, exc)
            continue
        if fp is None:
            continue
        entry: dict[str, Any] = {
            "font": basefont,
            "code": code,
            "glyph": name,
            "emits": emitted_char(font_info, code),
        }
        known = table.get(fp)
        if known is not None:
            resolved.append({**entry, "char": str(known["char"])})
        else:
            misses.append({**entry, "outline": fp})
    return resolved, misses


def resolve_document(
    doc: Any, fonts: list[dict[str, Any]], classify: Any
) -> dict[str, Any]:
    """Resolve every flagged simple font of an open PyMuPDF ``doc``.

    ``fonts`` are :func:`precis.ingest.glyph_health._font_info` records
    (each carrying its ``xref``); ``classify`` is
    :func:`~precis.ingest.glyph_health.classify_font`. Returns the
    ``recovery`` sub-record for ``meta['glyph_health']``; never raises.
    """
    resolved: list[dict[str, Any]] = []
    misses: list[dict[str, Any]] = []
    errors: list[str] = []
    for fi in fonts:
        modes = classify(fi) & {"a", "b", "symbolic_no_tounicode"}
        xref = fi.get("xref")
        if not modes or not xref or fi.get("subtype") not in _SIMPLE_SUBTYPES:
            continue
        try:
            _name, ext, _subtype, buf = doc.extract_font(xref)
            if not buf:
                continue  # not embedded: no outline to read
            program = open_font_program(buf, ext)
            got, missed = resolve_font(program, fi, modes)
        except Exception as exc:
            errors.append(f"{fi.get('basefont') or '?'}: {type(exc).__name__}")
            continue
        resolved.extend(got)
        misses.extend(missed)
    record: dict[str, Any] = {
        "scheme": FINGERPRINT_SCHEME,
        "resolved": resolved[:MAX_RESOLVED],
        "misses": misses[:MAX_MISSES],
    }
    if len(resolved) > MAX_RESOLVED:
        record["resolved_truncated"] = len(resolved) - MAX_RESOLVED
    if len(misses) > MAX_MISSES:
        record["misses_truncated"] = len(misses) - MAX_MISSES
    if errors:
        record["errors"] = errors[:MAX_ERRORS]
    return record


__all__ = [
    "FINGERPRINT_SCHEME",
    "FontProgram",
    "fingerprint_path",
    "open_font_program",
    "outline_table",
    "pdf_base_encoding",
    "resolve_document",
    "resolve_font",
    "suspect_codes",
]

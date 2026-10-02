"""Ingest-time glyph-health forensics (gr228652).

Publisher PDFs built with Elsevier/Advent-3B2 ``Adv*`` subset fonts
misdeclare their own glyph encoding, silently destroying ``μ``/Greek at
text-extraction time and turning meaning-changing unit errors (V/μm →
V/mm, 6 μm → 6 m, i.e. 10^3–10^6×) into clean-looking ASCII that no
downstream text scan can catch.

Two distinct failure modes, both settled in
``docs/backlog/ingest-strips-greek-glyphs.md``:

* **mode "a"** — a *lying* ``/ToUnicode``: the font (e.g.
  ``KKLGAD+AdvP7DA6``) names its μ glyph ``/m`` and maps code ``<6d>`` to
  ``<006D>``, so the extractor faithfully emits ASCII ``m`` with **zero
  residue**. Invisible at the text layer; only the font tables reveal it.
* **mode "b"** — no ``/ToUnicode`` and meaningless positional glyph names
  (e.g. ``IBDHKG+AdvP4C4E51`` glyph ``/C22``): the extractor falls back to
  the raw code and emits a C0 control char (U+0002). ``marker._clean_text``
  used to silently delete it, erasing the one visible marker.

Neither PyMuPDF nor pypdfium2/Marker can recover the true character, and
OCR is ruled out (see the backlog §"Ruling on recovery"). The only fix
available at ingest is **detection**: a per-document ``glyph_health``
record written onto the paper's ``meta`` so a flagged document can be
routed to a (separate, non-OCR) recovery pass and so a grounding audit can
tell "the source was corrupted at ingest" apart from "the claim is wrong".

Every text-level detector tried in the backlog was a base-rate failure
(absence of Greek is normal). The signals here are deterministic
**font-level** tests plus cheap supporting counts — not heuristics over
the stored text.

Read side: nothing recovers the text (detection only, by ruling), so every
surface that shows a flagged paper's content carries a caveat. The single
shared predicate :func:`glyph_suspected` and the one caveat sentence
:func:`glyph_caveat` live here, next to the record they interpret, and are
used by the paper views' banner, the finding view's ``source caveats:``
section and the draft write-path ``glyph_cite_hint``.

The classification helpers below are pure functions over already-parsed
font/geometry structures so they are unit-testable without a PDF engine;
:func:`analyze_pdf` is the thin, defensive PyMuPDF glue that feeds them.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Read side: predicate + caveat wording shared by every display surface
# ---------------------------------------------------------------------------


def glyph_suspected(meta: Any) -> bool:
    """True when ``meta`` (a ref's meta dict) carries a suspected
    ``glyph_health`` record. Tolerates ``None`` / non-dict / malformed."""
    if not isinstance(meta, dict):
        return False
    rec = meta.get("glyph_health")
    return isinstance(rec, dict) and rec.get("suspected") is True


def glyph_caveat(meta: Any) -> str:
    """One-line caveat for a glyph-suspected paper (``""`` when clean).

    Names the suspect fonts / modes only when the record carries them.
    """
    if not glyph_suspected(meta):
        return ""
    rec = meta["glyph_health"]
    detail: list[str] = []
    fonts = [str(f) for f in (rec.get("suspect_fonts") or [])]
    if fonts:
        detail.append("fonts: " + ", ".join(fonts))
    modes = [str(m) for m in (rec.get("modes") or [])]
    if modes:
        detail.append("modes: " + ", ".join(modes))
    suffix = f" ({'; '.join(detail)})" if detail else ""
    return (
        "Text extraction may have dropped or substituted Greek/\u03bc "
        f"characters in this paper{suffix}. Check numbers and units against "
        "the PDF before quoting."
    )


# ---------------------------------------------------------------------------
# Text-level signals (pure, no PDF engine required)
# ---------------------------------------------------------------------------

#: C0 control chars that :func:`precis.ingest.marker._clean_text` strips
#: (everything below 0x20 except TAB and LF). Kept byte-identical to the
#: marker regex on purpose — this is the count of mode-(b) residue that the
#: strip used to discard silently.
C0_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

#: Greek + micro-sign codepoints. Their total absence from a document that
#: otherwise carries symbols is the (weak on its own) precondition the
#: backlog documents; recorded as a supporting count, never used alone.
_GREEK_RE = re.compile(
    r"[Ͱ-Ͽἀ-῿µ]"  # Greek, Greek Extended, MICRO SIGN
)

#: Prose spelling of a micro-scale unit. A document that *says* "micron"
#: but contains no ``μ`` glyph is the near-zero-cost tell the backlog calls
#: the "highest-value signal" (pa494 says "few Volts per micron" three
#: sentences from ``V/mm``).
_MICRON_WORD_RE = re.compile(
    r"\b(?:micron|micrometer|micrometre|micrometre|micrometres|microns"
    r"|microsecond|microseconds|microgram|micrograms|micromolar)\b",
    re.IGNORECASE,
)

#: The Advent-3B2 ``Adv*`` font family named in the bug report. A subset
#: prefix (six caps + ``+``) is optional. Presence of any such font is the
#: publisher-property screen: the corruption "tracks the share of the
#: corpus published with Advent-3B2 symbol fonts".
_ADV_FONT_RE = re.compile(r"(?:^|\+)Adv[A-Za-z0-9]*", re.IGNORECASE)

#: Positional / subset glyph names that carry only a slot index and no
#: character identity: ``/C22``, ``/g17``, ``/glyph00022``, ``/cid123``,
#: ``/index5``. These are mode-(b)'s naming signature.
_POSITIONAL_GLYPH_RE = re.compile(
    r"^(?:c|g|gid|cid|glyph|index)[0-9a-f]+$", re.IGNORECASE
)


#: TeX math-extension fonts (Computer Modern ``cmex``, Latin Modern
#: ``lmex``, Euler ``euex``), subset prefix optional. Their low code points
#: are big delimiters and radicals — ``cmex10`` 0x00/0x01 are the large
#: parentheses — so a C0 char from one is an unmapped bracket, not a lost
#: letter. Counting them flagged a clean LaTeX paper (ref 461434, 12 C0 all
#: from CMEX8/CMEX10) on the first post-deploy ingest. CMMI's C0 range IS
#: lowercase Greek and still counts.
_MATH_EXTENSION_FONT_RE = re.compile(r"(?:^|\+)(?:CMEX|LMEX|EUEX)", re.IGNORECASE)


def count_c0_controls(text: str) -> int:
    """Count C0 control chars the ingest strip would delete (mode-(b) residue)."""
    return len(C0_CONTROL_RE.findall(text))


def count_c0_in_spans(text_dict: dict[str, Any]) -> int:
    """Count C0 control chars per span, skipping TeX math-extension fonts.

    Operates on PyMuPDF's ``page.get_text("dict")``. A C0 char set in a
    :data:`_MATH_EXTENSION_FONT_RE` font is a delimiter glyph with no
    ``/ToUnicode`` entry, not glyph loss, so it does not count.
    """
    total = 0
    for block in text_dict.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if _MATH_EXTENSION_FONT_RE.search(span.get("font") or ""):
                    continue
                total += count_c0_controls(span.get("text") or "")
    return total


def count_greek_chars(text: str) -> int:
    """Count Greek-range + micro-sign codepoints present in ``text``."""
    return len(_GREEK_RE.findall(text))


def count_micron_words_without_micro_sign(text: str) -> int:
    """Prose "micron"/"micrometer"/… mentions when the text has **no** ``μ``.

    Returns 0 as soon as any Greek/micro codepoint is present — the whole
    signal is "the paper talks about micro-scale but the μ glyph is gone".
    """
    if count_greek_chars(text) > 0:
        return 0
    return len(_MICRON_WORD_RE.findall(text))


def is_adv_family(basefont: str | None) -> bool:
    """True if ``basefont`` is an Advent-3B2 ``Adv*`` font (subset or not)."""
    return basefont is not None and _ADV_FONT_RE.search(basefont) is not None


def _is_positional_glyph_name(name: str) -> bool:
    """True for a slot-index glyph name (``C22``, ``g17``, ``glyph00022``)."""
    name = name.lstrip("/")
    return bool(_POSITIONAL_GLYPH_RE.match(name))


# ---------------------------------------------------------------------------
# Font-level classification (pure)
# ---------------------------------------------------------------------------
#
# ``font_info`` is a plain dict describing one embedded font, keyed:
#   basefont      : str          BaseFont name incl. any subset prefix
#   has_tounicode : bool         a /ToUnicode stream is present
#   has_encoding  : bool         an /Encoding (name or dict) is present
#   symbolic      : bool | None  Symbolic flag (FontDescriptor /Flags bit 3)
#   differences   : dict[int,str]  /Differences code -> glyph name (optional)
#   tounicode     : dict[int,str]  /ToUnicode code -> mapped char (optional)
#
# All keys except ``basefont`` are optional; classification degrades to the
# signals it can compute from what is present.


def _has_self_referential_tounicode(font_info: dict[str, Any]) -> bool:
    """Mode-(a) signature: a ``/ToUnicode`` that only echoes the glyph name.

    The lying font names its μ glyph ``/m`` and maps its code to U+006D —
    the ``/ToUnicode`` adds no information the glyph name did not already
    assert, so the extractor emits ``m`` for what is drawn as ``μ``. We
    detect the echo: a code whose ``/Differences`` name is a single-letter
    Adobe glyph name (``m``, ``M``, ``a`` …) and whose ``/ToUnicode`` maps
    that same code to exactly that one ASCII letter.

    On an honest text font this pattern is ubiquitous and meaningless,
    which is why callers gate it on the Adv* family (see
    :func:`classify_font`): a legitimate ``m`` is never isolated in its own
    single-glyph Advent subset font.
    """
    diffs = font_info.get("differences") or {}
    tou = font_info.get("tounicode") or {}
    if not diffs or not tou:
        return False
    for code, name in diffs.items():
        name = str(name).lstrip("/")
        if len(name) != 1 or not name.isalpha():
            continue
        mapped = tou.get(code)
        if mapped is not None and str(mapped) == name:
            return True
    return False


def classify_font(font_info: dict[str, Any]) -> set[str]:
    """Return the set of corruption-mode signatures a font matches.

    Possible members:

    * ``"a"`` — Adv* font with a self-referential (lying) ``/ToUnicode``.
      Detectable **only** in the font; produces clean ASCII with no
      residue, so a document carrying one must be flagged even when nothing
      else fires (the anti-masking regression case).
    * ``"b"`` — subset symbol font with no ``/ToUnicode`` and either no
      ``/Encoding`` or positional glyph names. Emits C0 residue.
    * ``"symbolic_no_tounicode"`` — supporting signal: a Symbolic font with
      no ``/ToUnicode`` (e.g. the ``≤``→``G`` ``AdvP0004`` case, which
      declares WinAnsi + a lying Nonsymbolic flag). Weaker on its own.
    * ``"adv_family"`` — publisher-property screen: the font is Advent-3B2.
    """
    modes: set[str] = set()
    basefont = font_info.get("basefont")
    has_tou = bool(font_info.get("has_tounicode"))
    has_enc = bool(font_info.get("has_encoding"))
    symbolic = font_info.get("symbolic")
    adv = is_adv_family(basefont)
    diffs = font_info.get("differences") or {}

    if adv:
        modes.add("adv_family")

    # mode (a): lying ToUnicode on an Adv* font. Gated on the Adv* family,
    # NOT on "subset font", on purpose: the self-referential echo (a glyph
    # named 'm' whose ToUnicode maps to 'm') is ubiquitous and honest on
    # ordinary subset text fonts, so flagging those is the whole-corpus
    # base-rate failure the backlog warns against. The family name is the
    # discriminator — a legitimate 'm' is never isolated in an Adv* subset.
    if adv and has_tou and _has_self_referential_tounicode(font_info):
        modes.add("a")

    # mode (b): no ToUnicode, and either no Encoding or positional names.
    if not has_tou:
        positional = bool(diffs) and all(
            _is_positional_glyph_name(n) for n in diffs.values()
        )
        if (adv and (not has_enc or positional)) or (not has_enc and positional):
            modes.add("b")

    # supporting: symbolic font with no ToUnicode (recovery is guesswork,
    # but it is a flag worth carrying).
    if symbolic and not has_tou:
        modes.add("symbolic_no_tounicode")

    return modes


# ---------------------------------------------------------------------------
# Geometry-level signal (pure): orphan single-char spans
# ---------------------------------------------------------------------------


def count_orphan_single_char_spans(text_dict: dict[str, Any]) -> int:
    """Count lone single-char spans whose font differs from both neighbours.

    Operates on PyMuPDF's ``page.get_text("dict")`` structure
    (blocks → lines → spans, each span carrying ``text`` and ``font``). A
    span that (a) holds exactly one non-space char and (b) sits between two
    other spans on the same line, both in a *different* font, is the
    signature of a symbol lifted into its own subset font mid-word — 100%
    precise on both backlog samples. This is what catches mode (a)'s μ,
    which leaves no other trace in the text.
    """
    orphans = 0
    for block in text_dict.get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            for i in range(1, len(spans) - 1):
                cur = spans[i]
                text = (cur.get("text") or "").strip()
                if len(text) != 1:
                    continue
                font = cur.get("font")
                prev_font = spans[i - 1].get("font")
                next_font = spans[i + 1].get("font")
                if font and font != prev_font and font != next_font:
                    orphans += 1
    return orphans


# ---------------------------------------------------------------------------
# Record assembly
# ---------------------------------------------------------------------------


def summarize(
    *,
    fonts: list[dict[str, Any]],
    c0_controls: int,
    greek_chars: int,
    micron_without_sign: int,
    orphan_spans: int,
    font_error: str | None = None,
) -> dict[str, Any]:
    """Combine the individual signals into the stored ``glyph_health`` record.

    ``suspected`` is set when any deterministic font-level signature fires
    (mode a/b or an Adv* font present) or when mode-(b) residue survives in
    the text. The corroborating text/geometry counts are recorded either
    way so a review pass can rank flagged documents.
    """
    modes: set[str] = set()
    suspect_fonts: list[str] = []
    for fi in fonts:
        fmodes = classify_font(fi)
        strong = fmodes - {"adv_family"}
        if strong or "adv_family" in fmodes:
            modes |= fmodes
            name = fi.get("basefont") or "?"
            if name not in suspect_fonts:
                suspect_fonts.append(name)

    strong_font_signal = bool(modes & {"a", "b"})
    adv_present = "adv_family" in modes

    suspected = bool(strong_font_signal or adv_present or c0_controls > 0)

    record: dict[str, Any] = {
        "suspected": suspected,
        "modes": sorted(m for m in modes if m in {"a", "b"}),
        "adv_family_present": adv_present,
        "c0_controls_before_strip": c0_controls,
        "greek_char_count": greek_chars,
        "micron_words_without_micro_sign": micron_without_sign,
        "orphan_single_char_spans": orphan_spans,
        "suspect_fonts": suspect_fonts,
    }
    if "symbolic_no_tounicode" in modes:
        record["symbolic_no_tounicode"] = True
    if font_error:
        record["font_analysis_error"] = font_error
    return record


def analyze_pdf(pdf_path: str | Path, extracted_text: str = "") -> dict[str, Any]:
    """Build the ``glyph_health`` record for ``pdf_path`` (defensive glue).

    Opens the PDF once with PyMuPDF to read font tables (BaseFont,
    ``/ToUnicode``/``/Encoding`` presence, Symbolic flag, ``/Differences``)
    and per-page geometry (orphan single-char spans), and counts C0 residue
    from the **raw** page text before any strip. All PyMuPDF work is
    best-effort: if the engine is missing or a document is unparseable the
    record still carries the text-level signals derived from
    ``extracted_text`` and a ``font_analysis_error`` note, never raising.
    """
    fonts: list[dict[str, Any]] = []
    orphan_spans = 0
    raw_c0 = 0
    font_error: str | None = None

    try:
        import fitz  # PyMuPDF; a paper-extra, imported lazily like elsewhere.

        with fitz.open(str(pdf_path)) as doc:
            seen_xrefs: set[int] = set()
            for page in doc:
                try:
                    text_dict = page.get_text("dict")
                    raw_c0 += count_c0_in_spans(text_dict)
                    orphan_spans += count_orphan_single_char_spans(text_dict)
                    for font in page.get_fonts(full=True):
                        xref = font[0]
                        if xref in seen_xrefs:
                            continue
                        seen_xrefs.add(xref)
                        fonts.append(_font_info(doc, font))
                except Exception as exc:  # one bad page must not sink the record
                    log.debug("glyph_health: page scan failed: %s", exc)
    except ImportError:
        font_error = "pymupdf (fitz) unavailable"
    except Exception as exc:  # unparseable / encrypted PDF, etc.
        font_error = f"{type(exc).__name__}: {exc}"
        log.debug("glyph_health: font analysis failed for %s: %s", pdf_path, exc)

    # C0 residue survives extraction only in mode (b); a lying-ToUnicode
    # mode-(a) document has none. The per-span raw count is authoritative
    # when the PDF opened: it can tell a math-extension delimiter from a lost
    # letter, and the extracted text cannot, so it is only the fallback when
    # the engine is missing or the document would not parse.
    c0 = raw_c0 if font_error is None else count_c0_controls(extracted_text)

    return summarize(
        fonts=fonts,
        c0_controls=c0,
        greek_chars=count_greek_chars(extracted_text),
        micron_without_sign=count_micron_words_without_micro_sign(extracted_text),
        orphan_spans=orphan_spans,
        font_error=font_error,
    )


def _font_info(doc: Any, font: tuple[Any, ...]) -> dict[str, Any]:
    """Parse one ``page.get_fonts(full=True)`` entry into a ``font_info`` dict.

    Reads the raw font object (and its FontDescriptor) via ``xref_object``
    to determine ``/ToUnicode``/``/Encoding`` presence, the Symbolic flag,
    and ``/Differences`` glyph names, plus the ``/ToUnicode`` ``bfchar``
    map when cheaply parseable. Every field is best-effort.
    """
    xref = font[0]
    basefont = font[3] if len(font) > 3 else None
    encoding = font[5] if len(font) > 5 else ""

    info: dict[str, Any] = {
        "basefont": basefont,
        "has_tounicode": False,
        "has_encoding": bool(encoding),
        "symbolic": None,
    }
    try:
        obj = doc.xref_object(xref, compressed=True)
    except Exception:
        return info

    info["has_tounicode"] = "/ToUnicode" in obj
    if "/Encoding" in obj:
        info["has_encoding"] = True

    # Symbolic flag lives in the FontDescriptor's /Flags (bit 3 == 4).
    fd_match = re.search(r"/FontDescriptor\s+(\d+)\s+\d+\s+R", obj)
    if fd_match:
        try:
            fd = doc.xref_object(int(fd_match.group(1)), compressed=True)
            flags_match = re.search(r"/Flags\s+(\d+)", fd)
            if flags_match:
                info["symbolic"] = bool(int(flags_match.group(1)) & 4)
        except Exception:
            pass

    # /Differences: [ 34 /C22 35 /C23 ... ] — parse the name run.
    diff_match = re.search(r"/Differences\s*\[([^\]]*)\]", obj)
    if diff_match:
        info["differences"] = _parse_differences(diff_match.group(1))

    # /ToUnicode bfchar map, when the stream is small and parseable.
    if info["has_tounicode"]:
        tou_match = re.search(r"/ToUnicode\s+(\d+)\s+\d+\s+R", obj)
        if tou_match:
            try:
                stream = doc.xref_stream(int(tou_match.group(1)))
                info["tounicode"] = _parse_tounicode(stream)
            except Exception:
                pass
    return info


def _parse_differences(body: str) -> dict[int, str]:
    """Parse a PDF ``/Differences`` array body into ``{code: glyph_name}``."""
    diffs: dict[int, str] = {}
    current: int | None = None
    for tok in re.findall(r"\d+|/[^\s/\[\]]+", body):
        if tok[0] == "/":
            if current is not None:
                diffs[current] = tok[1:]
                current += 1
        else:
            current = int(tok)
    return diffs


def _parse_tounicode(stream: bytes | str) -> dict[int, str]:
    """Parse ``bfchar`` entries of a ``/ToUnicode`` CMap into ``{code: char}``.

    Handles the single-code ``<6d> <006D>`` form that the lying Adv* fonts
    use. Ranges and multi-char targets are ignored (not needed for the
    self-referential-echo test).
    """
    if isinstance(stream, bytes):
        text = stream.decode("latin-1", "replace")
    else:
        text = stream
    out: dict[int, str] = {}
    for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", text):
        try:
            code = int(src, 16)
            # UTF-16BE codepoints, 4 hex digits each.
            chars = "".join(chr(int(dst[i : i + 4], 16)) for i in range(0, len(dst), 4))
            out[code] = chars
        except ValueError:
            continue
    return out

"""Tests for gr228652 — ingest-time glyph-health forensics.

The Advent-3B2 ``Adv*`` fonts silently destroy μ/Greek at extraction in two
modes; these exercise the deterministic classification/counting logic that
detects them. The PyMuPDF glue (:func:`analyze_pdf`) needs a real PDF engine
and is not unit-tested here — the pure helpers it delegates to are.
"""

from __future__ import annotations

from precis.ingest.glyph_health import (
    _has_self_referential_tounicode,
    _is_positional_glyph_name,
    _parse_differences,
    _parse_tounicode,
    classify_font,
    count_bare_unit_numbers,
    count_c0_controls,
    count_c0_in_spans,
    count_greek_chars,
    count_micron_words_without_micro_sign,
    count_orphan_single_char_spans,
    count_stray_space_units,
    count_symbol_chars,
    has_positive_tell,
    is_adv_family,
    summarize,
)

# ── Text-level counts ────────────────────────────────────────────────


class TestC0Counts:
    def test_counts_mode_b_residue(self) -> None:
        # "6 \x02m" is pa47024's stored-before-strip shape.
        assert count_c0_controls("6 \x02m/s and 7.3 \x02m") == 2

    def test_tab_and_newline_are_not_controls(self) -> None:
        assert count_c0_controls("a\tb\nc") == 0

    def test_clean_text_has_none(self) -> None:
        assert count_c0_controls("perfectly clean ascii") == 0


class TestC0InSpans:
    """Per-span C0 count skips TeX math-extension delimiters (ref 461434)."""

    @staticmethod
    def _dict(*spans: tuple[str, str]) -> dict:
        return {
            "blocks": [
                {"lines": [{"spans": [{"font": f, "text": t} for f, t in spans]}]}
            ]
        }

    def test_cmex_delimiters_do_not_count(self) -> None:
        # cmex10 0x00/0x01 = big parens; a clean LaTeX paper emits these.
        d = self._dict(("CMR10", "f"), ("CMEX10", "\x00"), ("ABCDEF+CMEX8", "\x10"))
        assert count_c0_in_spans(d) == 0

    def test_cmmi_c0_still_counts(self) -> None:
        # CMMI's C0 range is lowercase Greek: that is real glyph loss.
        d = self._dict(("CMMI10", "\x0b"), ("LMEX10", "\x01"), ("AdvP7DA6", "\x02"))
        assert count_c0_in_spans(d) == 2

    def test_empty_dict(self) -> None:
        assert count_c0_in_spans({}) == 0


class TestGreekCounts:
    def test_counts_greek_and_micro(self) -> None:
        # μ (U+03BC), µ (U+00B5 MICRO SIGN), α, Ω
        assert count_greek_chars("μm µV α Ω") == 4

    def test_zero_when_absent(self) -> None:
        assert count_greek_chars("plain ascii 6 m/s") == 0


class TestMicronProse:
    def test_fires_when_micron_word_but_no_micro_sign(self) -> None:
        # pa494: "few Volts per micron" three sentences from V/mm.
        assert (
            count_micron_words_without_micro_sign(
                "threshold field values of few Volts per micron"
            )
            == 1
        )

    def test_suppressed_when_micro_sign_present(self) -> None:
        # If the μ survived, the prose mention is not a scar.
        assert count_micron_words_without_micro_sign("6 μm, i.e. six microns") == 0

    def test_zero_when_no_micron_word(self) -> None:
        assert count_micron_words_without_micro_sign("100 mm thick membrane") == 0


class TestSymbolCount:
    def test_counts_kept_symbols(self) -> None:
        assert count_symbol_chars("1.4 ± 0.1 Å at 20 °C × 2") == 4

    def test_drops_en_and_em_dashes(self) -> None:
        # Dashes alone drove 30/30 false positives in the 10-14 sample —
        # they are ordinary range typography, not the symbol precondition.
        assert count_symbol_chars("pp. 12–20 — and 2020–2025") == 0


class TestBareUnitNumbers:
    def test_counts_stripped_micro_metres(self) -> None:
        # pa47024: "6 m diameter", "7.3 m/s", "35 m for a cavity radius
        # of 75 m", "30 m s−1" — a lost μm reads as plain metres.
        assert count_bare_unit_numbers("6 m diameter, 7.3 m/s, 35 m and 75 m") == 4

    def test_ignores_milli_units_and_square_metres(self) -> None:
        assert (
            count_bare_unit_numbers(
                "10 mA, 100 mm, 5 ms, 3 m², 20 mV, 3500 m<sup>2</sup>/g"
            )
            == 0
        )

    def test_ignores_micro_sign(self) -> None:
        assert count_bare_unit_numbers("6 μm and 6 µm") == 0


class TestStraySpaceUnits:
    def test_counts_stray_space_unit(self) -> None:
        # pa494: "1–2 V/ mm" for "V/µm" — the space stands where the
        # broken glyph sat.
        assert count_stray_space_units("threshold fields 1–2 V/ mm") == 1

    def test_ignores_compact_unit(self) -> None:
        assert count_stray_space_units("1 V/mm is correct") == 0


class TestPositiveTell:
    def test_pa494_shape(self) -> None:
        assert has_positive_tell(
            "threshold field values of few Volts per micron "
            "are in the range of 1–2 V/ mm"
        )

    def test_pa47024_shape(self) -> None:
        assert has_positive_tell(
            "untethered mobile milli/microrobots, micrometre scale, "
            "microparticles with 6 m diameter"
        )

    def test_dash_only_text_no_tell(self) -> None:
        assert not has_positive_tell(
            "published 2010–2020 — pages 12–20, see refs [3]–[7]"
        )

    def test_micron_word_without_bare_number_no_tell(self) -> None:
        assert not has_positive_tell("the membrane thickness was several microns")


# ── Font family / glyph name recognition ─────────────────────────────


class TestAdvFamily:
    def test_recognizes_subset_adv_fonts(self) -> None:
        assert is_adv_family("KKLGAD+AdvP7DA6")
        assert is_adv_family("IBDHKG+AdvP4C4E51")
        assert is_adv_family("IBEEBP+AdvP0004")

    def test_recognizes_unsubsetted(self) -> None:
        assert is_adv_family("AdvTT12345")

    def test_rejects_normal_fonts(self) -> None:
        assert not is_adv_family("ABCDEF+TimesNewRoman")
        assert not is_adv_family("MPDFAA+NotoSans")
        assert not is_adv_family(None)


class TestPositionalGlyphNames:
    def test_positional(self) -> None:
        assert _is_positional_glyph_name("C22")
        assert _is_positional_glyph_name("/C22")
        assert _is_positional_glyph_name("g17")
        assert _is_positional_glyph_name("glyph00022")
        assert _is_positional_glyph_name("cid123")

    def test_real_names(self) -> None:
        assert not _is_positional_glyph_name("mu")
        assert not _is_positional_glyph_name("m")
        assert not _is_positional_glyph_name("space")


# ── /Differences and /ToUnicode parsing ──────────────────────────────


class TestParsers:
    def test_parse_differences(self) -> None:
        assert _parse_differences("34 /C22 35 /C23 40 /m") == {
            34: "C22",
            35: "C23",
            40: "m",
        }

    def test_parse_differences_consecutive(self) -> None:
        # A single code followed by several names increments the code.
        assert _parse_differences("97 /a /b /c") == {97: "a", 98: "b", 99: "c"}

    def test_parse_tounicode_single(self) -> None:
        # pa494's lying map: <6d> -> <006D>.
        assert _parse_tounicode("<6d> <006D>") == {0x6D: "m"}

    def test_parse_tounicode_bytes(self) -> None:
        assert _parse_tounicode(b"beginbfchar\n<41> <0041>\nendbfchar") == {0x41: "A"}


# ── Self-referential (mode a) detection ──────────────────────────────


class TestSelfReferential:
    def test_pa494_lying_map_is_self_referential(self) -> None:
        # glyph named 'm', ToUnicode maps its code to 'm' — echoes the name.
        fi = {
            "differences": {0x6D: "m"},
            "tounicode": {0x6D: "m"},
        }
        assert _has_self_referential_tounicode(fi)

    def test_honest_mu_glyph_not_echoed(self) -> None:
        # An honest font naming its glyph 'mu' and mapping to μ is not the
        # single-letter echo pattern.
        fi = {"differences": {0x6D: "mu"}, "tounicode": {0x6D: "μ"}}
        assert not _has_self_referential_tounicode(fi)

    def test_no_data(self) -> None:
        assert not _has_self_referential_tounicode({})


# ── Font classification ──────────────────────────────────────────────


class TestClassifyFont:
    def test_mode_a_lying_tounicode(self) -> None:
        # pa494 / KKLGAD+AdvP7DA6 — the hard, residue-free mode.
        fi = {
            "basefont": "KKLGAD+AdvP7DA6",
            "has_tounicode": True,
            "has_encoding": True,
            "symbolic": True,
            "differences": {0x6D: "m"},
            "tounicode": {0x6D: "m"},
        }
        assert "a" in classify_font(fi)

    def test_mode_b_no_tounicode_positional(self) -> None:
        # pa47024 / IBDHKG+AdvP4C4E51 — no ToUnicode, /C22 glyph names.
        fi = {
            "basefont": "IBDHKG+AdvP4C4E51",
            "has_tounicode": False,
            "has_encoding": False,
            "symbolic": True,
            "differences": {34: "C22"},
        }
        modes = classify_font(fi)
        assert "b" in modes
        assert "symbolic_no_tounicode" in modes

    def test_symbolic_no_tounicode_winansi(self) -> None:
        # AdvP0004 — declares WinAnsi + lying Nonsymbolic flag; ≤ -> G.
        # It has an /Encoding so it is not mode (b), but it IS an Adv font
        # with no ToUnicode: still caught by the family screen.
        fi = {
            "basefont": "IBEEBP+AdvP0004",
            "has_tounicode": False,
            "has_encoding": True,
            "symbolic": False,
        }
        modes = classify_font(fi)
        assert "adv_family" in modes

    def test_honest_font_not_flagged(self) -> None:
        fi = {
            "basefont": "ABCDEF+TimesNewRoman",
            "has_tounicode": True,
            "has_encoding": True,
            "symbolic": False,
            "differences": {0x6D: "m"},
            "tounicode": {0x6D: "m"},
        }
        # A normal text font whose 'm' maps to 'm' must NOT be flagged mode a
        # (this is the whole-corpus false-positive trap the backlog warns of).
        assert classify_font(fi) == set()


# ── Orphan single-char spans (mode a geometry signal) ────────────────


class TestOrphanSpans:
    def test_lone_symbol_span_between_two_other_fonts(self) -> None:
        # "V/ μ m" where μ is its own subset-font span mid-token.
        td = {
            "blocks": [
                {
                    "lines": [
                        {
                            "spans": [
                                {"text": "V/", "font": "Times"},
                                {"text": "μ", "font": "AdvP7DA6"},
                                {"text": "m", "font": "Times"},
                            ]
                        }
                    ]
                }
            ]
        }
        assert count_orphan_single_char_spans(td) == 1

    def test_uniform_font_line_has_no_orphans(self) -> None:
        td = {
            "blocks": [
                {
                    "lines": [
                        {
                            "spans": [
                                {"text": "abc", "font": "Times"},
                                {"text": "d", "font": "Times"},
                                {"text": "efg", "font": "Times"},
                            ]
                        }
                    ]
                }
            ]
        }
        assert count_orphan_single_char_spans(td) == 0


# ── Record assembly & anti-masking guarantee ─────────────────────────


class TestSummarize:
    def test_mode_a_suspected_with_zero_residue(self) -> None:
        """The anti-masking regression (backlog §"Masking risk").

        A pa494-shaped document — lying ToUnicode, ZERO control-char
        residue, clean ASCII — must still be flagged. A control-char-only
        patch would fail this and hide mode (a) behind a "fixed" commit.
        """
        rec = summarize(
            fonts=[
                {
                    "basefont": "KKLGAD+AdvP7DA6",
                    "has_tounicode": True,
                    "has_encoding": True,
                    "symbolic": True,
                    "differences": {0x6D: "m"},
                    "tounicode": {0x6D: "m"},
                }
            ],
            c0_controls=0,  # no residue whatsoever
            greek_chars=0,
            micron_without_sign=1,
            orphan_spans=1,
        )
        assert rec["suspected"] is True
        assert rec["modes"] == ["a"]

    def test_mode_b_suspected(self) -> None:
        rec = summarize(
            fonts=[
                {
                    "basefont": "IBDHKG+AdvP4C4E51",
                    "has_tounicode": False,
                    "has_encoding": False,
                    "symbolic": True,
                    "differences": {34: "C22"},
                }
            ],
            c0_controls=5,
            greek_chars=0,
            micron_without_sign=0,
            orphan_spans=0,
        )
        assert rec["suspected"] is True
        assert "b" in rec["modes"]
        assert rec["c0_controls_before_strip"] == 5

    def test_clean_document_not_suspected(self) -> None:
        rec = summarize(
            fonts=[
                {
                    "basefont": "ABCDEF+TimesNewRoman",
                    "has_tounicode": True,
                    "has_encoding": True,
                    "symbolic": False,
                }
            ],
            c0_controls=0,
            greek_chars=12,
            micron_without_sign=0,
            orphan_spans=0,
        )
        assert rec["suspected"] is False
        assert rec["modes"] == []
        assert rec["suspect_fonts"] == []

    def test_c0_residue_alone_not_suspected(self) -> None:
        # Tightened 2026-10-14 (gr228594): surviving residue WITHOUT a
        # positive tell no longer flags — benign dash-emitting fonts left
        # exactly this signature in the 30/30 false-positive sample.
        rec = summarize(
            fonts=[],
            c0_controls=3,
            greek_chars=0,
            micron_without_sign=0,
            orphan_spans=0,
            font_error="pymupdf (fitz) unavailable",
        )
        assert rec["suspected"] is False
        assert rec["font_analysis_error"] == "pymupdf (fitz) unavailable"

    def test_c0_residue_with_tell_suspected(self) -> None:
        # The same residue plus a spelled micro-unit word AND a bare-m
        # quantity is the pa47024-shaped scar, and it flags.
        rec = summarize(
            fonts=[],
            c0_controls=3,
            greek_chars=0,
            micron_without_sign=2,
            orphan_spans=0,
            bare_unit_numbers=5,
            font_error="pymupdf (fitz) unavailable",
        )
        assert rec["suspected"] is True

    def test_dash_only_paper_not_suspected(self) -> None:
        # The 30/30 FP shape: an honestly-encoded Adv* font present (its
        # dashes/ranges set in the publisher's symbol font), no tell.
        rec = summarize(
            fonts=[
                {
                    "basefont": "KKLGAD+AdvPSTOT",
                    "has_tounicode": True,
                    "has_encoding": True,
                }
            ],
            c0_controls=2,
            greek_chars=0,
            micron_without_sign=0,
            orphan_spans=0,
            symbol_chars=0,
        )
        assert rec["suspected"] is False
        assert rec["adv_family_present"] is True

    def test_symbol_precondition_with_tell_suspected(self) -> None:
        # Kept symbols (± ° × Å) + zero Greek + a positive tell flags
        # even with no font evidence — the cheap-version precondition.
        rec = summarize(
            fonts=[],
            c0_controls=0,
            greek_chars=0,
            micron_without_sign=1,
            orphan_spans=0,
            symbol_chars=4,
            bare_unit_numbers=1,
        )
        assert rec["suspected"] is True

    def test_stray_space_tell_suspected(self) -> None:
        # pa494's "V/ mm" alone is a positive tell with weak evidence.
        rec = summarize(
            fonts=[],
            c0_controls=1,
            greek_chars=0,
            micron_without_sign=1,
            orphan_spans=0,
            stray_space_units=2,
        )
        assert rec["suspected"] is True

    def test_micron_word_without_bare_unit_not_suspected(self) -> None:
        # A spelled "micron" alone is not a tell — the bare-m quantity is
        # the positive half; a paper talking about microns in correct
        # units stays clean.
        rec = summarize(
            fonts=[
                {
                    "basefont": "KKLGAD+AdvPSTOT",
                    "has_tounicode": True,
                    "has_encoding": True,
                }
            ],
            c0_controls=0,
            greek_chars=0,
            micron_without_sign=3,
            orphan_spans=0,
        )
        assert rec["suspected"] is False

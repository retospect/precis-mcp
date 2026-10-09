"""LaTeX export for the draft kind (the Tier-B export path).

Pure-render unit tests for the inline converter / bib / acronym builders
(no DB), plus an end-to-end ``export_draft`` against real Postgres via
the ``hub`` fixture.
"""

from __future__ import annotations

import base64
import re
import sys
from pathlib import Path

import pytest

from precis.export import latex
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub
from precis.utils import handle_registry as _handle_registry

# A real 1×1 PNG for figure-embed tests (a valid raster blob).
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAA"
    "C0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_PNG_B64 = base64.b64encode(_PNG).decode()

# The compile tests drive a ``#!/bin/sh`` stub through ``shutil.which`` +
# ``subprocess.run``. On Windows ``shutil.which`` won't treat an
# extension-less file as executable (no ``PATHEXT`` match), and the POSIX
# shebang can't be invoked as a native binary — so the stub-binary pattern
# is POSIX-only. Same family of skip as ``tests/test_claude_agent.py``.
_needs_posix_stub = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX execute-shebang support required for the latexmk stub-binary pattern",
)

# ── inline rendering (no DB) ──────────────────────────────────────────


def _ctx(text, abbrevs=None, known=None, store=None, legacy_to_dc=None):
    """Build a render context. ``known_handles`` auto-includes every
    ``dc<id>`` handle in the text (so rendering tests don't trip the
    dangling-xref downgrade) unless the caller pins a set to exercise that
    path."""
    if known is None:
        known = set(re.findall(r"\b(dc\d+)\b", text))
    return latex._Ctx(
        keymap=latex._acronym_keymap(abbrevs or {}),
        abbrevs=dict(abbrevs or {}),
        known_handles=known,
        store=store,
        legacy_to_dc=legacy_to_dc or {},
    )


def _inline(text, abbrevs=None, known=None, store=None, legacy_to_dc=None):
    ctx = _ctx(text, abbrevs, known, store, legacy_to_dc)
    out = latex._render_inline(text, ctx)
    return out, ctx


class _PaperStore:
    """Minimal store: resolves a paper handle to its cite_key."""

    def resolve_handle(self, h):
        from precis.store.types import ResolvedHandle

        if h in ("pc10", "pa99"):
            return ResolvedHandle(
                ref_id=99, kind="paper", public_id="kong24", chunk_id=10
            )
        return None


class _FakeNonHubConn:
    """Stub connection whose ``.execute(...).fetchone()`` always returns
    ``None`` — used by :class:`_FindingStore` so
    ``taproot.seniority.is_claim_hub`` sees "not a hub" without a real DB."""

    def execute(self, *_args, **_kwargs):
        return self

    def fetchone(self):
        return None


class _FakeNonHubPool:
    def connection(self):
        import contextlib

        return contextlib.nullcontext(_FakeNonHubConn())


class _FindingStore:
    """Minimal store for a plain (non-hub) finding handle: resolves
    ``fetch_refs_by_ids`` from a caller-supplied ``{ref_id: ref}`` map, and
    a ``pool`` stub so :func:`precis.taproot.seniority.is_claim_hub`'s
    real-DB tag check sees "not a hub" without a Postgres connection."""

    def __init__(self, refs):
        self._refs = refs
        self.pool = _FakeNonHubPool()

    def fetch_refs_by_ids(self, ids):
        return {i: self._refs[i] for i in ids if i in self._refs}

    def tags_for(self, _ref_id):
        # No STATUS tag → precis.taproot.trust defaults to 'tracing'
        # (unverified/"source pending") — these tests exercise cite-key
        # resolution, not the trust mark, so an untagged fixture is fine.
        return []


def test_escapes_latex_specials() -> None:
    out, _ = _inline("100% pure & cheap_at $5 #1 {x}")
    # bare $…$ with no closing pairs up: "$5 #1 {x}" has no second $, so
    # the whole run is escaped (no math span).
    assert r"\%" in out and r"\&" in out and r"\_" in out and r"\#" in out
    assert r"\{x\}" in out


def test_math_passthrough_not_escaped() -> None:
    out, _ = _inline("the rate $k_\\mathrm{obs} = 2$ and aside")
    assert "$k_\\mathrm{obs} = 2$" in out  # verbatim, underscores intact


def test_bold_code_sub_sup() -> None:
    out, _ = _inline("see **2.6 mmol** `code_x` and NH<sub>2</sub> g<sup>-1</sup>")
    assert r"\textbf{2.6 mmol}" in out
    assert r"\texttt{code\_x}" in out
    assert r"\textsubscript{2}" in out and r"\textsuperscript{-1}" in out


def test_unbalanced_math_escaped_not_passed_through() -> None:
    # Garbled PDF-extracted math (seven \sqrt{ closed six times, prod chunk
    # 194080) must NOT pass through verbatim — inside a \footnote{…} the
    # stray brace swallows the rest of the document and kills the compile.
    garbled = r"ratios $q_x/q_1 = \sqrt{1:\sqrt{2:\sqrt{3:\sqrt{4}}}$ here"
    out, _ = _inline(garbled)
    assert "\\sqrt{" not in out  # not passed through verbatim
    assert "$" not in out.replace(r"\$", "")  # no live math delimiter survived
    # grouping braces balance once escaped \{ \} glyphs are set aside
    stripped = out.replace(r"\{", "").replace(r"\}", "")
    assert stripped.count("{") == stripped.count("}")
    # balanced math in the same run still passes through untouched
    out2, _ = _inline(r"fine $\sqrt{2}$ and $x_{1}$")
    assert r"$\sqrt{2}$" in out2 and "$x_{1}$" in out2
    # an escaped \{ is a glyph, not grouping — the span counts as balanced
    # and passes through verbatim (kills the escape-tracking mutants)
    out3, _ = _inline(r"interval $a \{ b$ end")
    assert r"$a \{ b$" in out3


def test_cyrillic_homoglyphs_map_to_latin_never_cyr_commands() -> None:
    # PDF extraction drops Cyrillic lookalikes into English prose; pylatexenc's
    # default \CYRT-style commands need T2A fontenc the preamble doesn't load —
    # a fatal "Undefined control sequence" (prod job 338757). Homoglyphs map to
    # the identical Latin letter; other Cyrillic stays verbatim (recoverable).
    out, _ = _inline("wick-vapor interface Т temperature, СО2 uptake")
    assert "\\CYR" not in out and "\\cyr" not in out
    assert "interface T temperature" in out
    assert "CO2 uptake" in out
    out2, _ = _inline("genuine Cyrillic я stays verbatim")
    assert "я" in out2 and "\\cyr" not in out2


def test_math_command_split_from_following_unicode_letter() -> None:
    # Under LuaTeX a Unicode letter extends a control-sequence name, so
    # $\Deltaδ$ parses as ONE undefined command (fatal). A {} separator
    # preserves the rendering the author meant. δ itself is now ALSO
    # translated to its math command (gr339 Unicode-in-math fix) rather
    # than riding verbatim — raw δ against LuaLaTeX's math font is exactly
    # the missing-character degrade this fix closes.
    out, _ = _inline("shift changes of $\\Deltaδ ≈0.5$ ppm")
    assert "\\Delta{}{\\delta}" in out
    assert "δ" not in out


def _no_raw_math_unicode(out: str, chars: str) -> None:
    """None of *chars* survived as a raw glyph anywhere in *out* — the
    "Missing character" signature this whole fix exists to kill."""
    for ch in chars:
        assert ch not in out, f"{ch!r} rode through raw: {out!r}"


def test_unicode_inside_math_span_gets_math_mode_macros() -> None:
    # gr339: pylatexenc's Unicode→LaTeX table only ever ran on PROSE —
    # math spans are stashed verbatim past it. Raw ≠/°/α/Å/… inside $…$
    # hits LuaLaTeX's math font table with no such glyph (real compile:
    # "Missing character: There is no ≠ (U+2260) in font rm-lmr10!",
    # "$3 \times 120° = 360°$" → "3 × 120ř = 360ř"). Each case here is
    # _math_plausible-TRUE (goes through the verbatim math-stash path,
    # the one that was silently dropping Unicode) — see the decoupled
    # variants below for the equally-required FALSE-classifier cases.
    out, _ = _inline("$K ≠0$")
    _no_raw_math_unicode(out, "≠")
    assert r"\neq" in out

    out, _ = _inline("$3 \\times 120° = 360°$")
    _no_raw_math_unicode(out, "°")
    assert out.count("^{\\circ}") == 2  # exponent form, both occurrences

    out, _ = _inline("$5 Å$")
    _no_raw_math_unicode(out, "Å")
    assert r"\mathring{A}" in out

    out, _ = _inline("$a × b$")
    _no_raw_math_unicode(out, "×")
    assert r"\times" in out

    out, _ = _inline("$a ± b$")
    _no_raw_math_unicode(out, "±")
    assert r"\pm" in out

    out, _ = _inline("$90^°$")
    _no_raw_math_unicode(out, "°")
    assert "90^{\\circ}" in out  # author's own caret, not doubled

    out, _ = _inline("$μ$")
    _no_raw_math_unicode(out, "μ")
    assert r"\mu" in out

    out, _ = _inline("$Δx$")
    _no_raw_math_unicode(out, "Δ")
    assert "{\\Delta}x" in out  # brace-terminated: doesn't merge into \Deltax

    out, _ = _inline("$−5$")  # U+2212 MINUS SIGN, not a hyphen
    _no_raw_math_unicode(out, "−")
    assert "{-}5" in out

    out, _ = _inline("$∼0.9$")
    _no_raw_math_unicode(out, "∼")
    assert r"\sim" in out

    out, _ = _inline("$x′$")
    _no_raw_math_unicode(out, "′")
    assert "x'" in out


def test_unicode_inside_math_decoupled_from_math_plausible_verdict() -> None:
    # Both spacings of the SAME symbol must render correctly — proving the
    # fix doesn't depend on which side of _math_plausible's heuristic the
    # span lands on. "$K ≠ 0$"/"$a ≤ b$"/"$α + β$" (2+ spaces, no char in
    # _math_plausible's mathy-char set) are classifier-FALSE today and fall
    # through to the ordinary prose escape — which already correctly
    # Unicode-encodes (this was never broken); the no-space/1-space
    # siblings are classifier-TRUE and hit the new math-mode path. Neither
    # \_math_plausible itself is touched or asserted on here.
    for spaced, tight in [
        ("$K ≠ 0$", "$K ≠0$"),
        ("$a ≤ b$", "$a≤b$"),
        ("$α + β$", "$α+β$"),
    ]:
        out_spaced, _ = _inline(spaced)
        out_tight, _ = _inline(tight)
        for out, src in [(out_spaced, spaced), (out_tight, tight)]:
            for ch in "≠≤αβ":
                assert ch not in out, f"{ch!r} raw in {src!r} -> {out!r}"
    out, _ = _inline("$K ≠ 0$")
    assert r"\neq" in out
    out, _ = _inline("$a ≤ b$")
    assert r"\leq" in out
    out, _ = _inline("$α + β$")
    assert r"\alpha" in out and r"\beta" in out


def test_unicode_inside_math_negative_controls() -> None:
    # Currency $ untouched — not remotely math, no unicode involved, must
    # not be mangled by this change.
    out, _ = _inline(r"Scaffold \$300 + 200 staples \$200; done")
    assert r"\$300" in out and r"\$200" in out

    # Cyrillic/Hebrew inside math: 2628caa2's raw-script exception must
    # still hold — a homoglyph (this Cyrillic К is pixel-identical to
    # Latin K) normalises, a non-homoglyph Cyrillic/Hebrew letter stays
    # raw rather than emitting an undefined \CYR.../font-encoding command.
    out, _ = _inline("$К ≠ 0$")
    assert "K" in out and "\\CYR" not in out and "\\cyr" not in out
    out, _ = _inline("$Ж ≠ 0$")
    assert "Ж" in out and "\\CYR" not in out and "\\cyr" not in out
    out, _ = _inline("$שלום ≠ 0$")
    assert "שלום" in out and "\\hebrew" not in out.lower()

    # Prose-mode degree signs OUTSIDE math are untouched by this change —
    # still the existing \textdegree (bare-degree, not the math \circ
    # exponent form only valid inside $…$).
    out, _ = _inline("it was 90° outside")
    assert r"\textdegree" in out
    assert "\\circ" not in out


def test_ampersand_inside_math_escaped_outside_alignments() -> None:
    # Pseudocode bitwise & inside math ("$f = a & b$") is a "Misplaced
    # alignment tab" fatal outside a real alignment environment.
    out, _ = _inline("mask $f = a & b$ done")
    assert r"$f = a \& b$" in out
    # …but a genuine \begin{matrix} keeps its structural tabs.
    out2, _ = _inline(r"$\begin{matrix} a & b \end{matrix}$")
    assert r"a & b" in out2


def test_escaped_dollar_is_a_literal_never_a_math_delimiter() -> None:
    # Author-escaped \$ is a money dollar: two of them must not pair into a
    # "math" span (that stranded a bare $ that opened math for the rest of
    # the document), and the rendering is \$ (a plain $), not
    # \textbackslash{}\$.
    out, _ = _inline(r"Scaffold \$300 + 200 staples \$200; done")
    assert r"\$300" in out and r"\$200" in out
    assert "textbackslash" not in out
    assert "$" not in out.replace(r"\$", "")


def test_implausible_wordy_math_span_is_escaped() -> None:
    # Two stray money-dollars pairing across half a sentence is not math —
    # the span falls through to the escaper (literal dollars) instead of
    # emitting live $s that break the compile downstream.
    out, _ = _inline("run $30 cycles then produces more copies $ ok")
    assert "$" not in out.replace(r"\$", "")


def test_adjacent_math_spans_do_not_glue_into_display_math() -> None:
    # $∼$$2^30$ (authored) restores as two adjacent spans — a bare $$ is a
    # display-math opener fatal; a {} separator keeps both inline.
    out, _ = _inline("produces $∼$$2^{30}$ copies")
    assert "$$" not in out
    # adjacent non-math stashes (two code spans) need no separator
    out2, _ = _inline("`a``b`")
    assert r"\texttt{a}\texttt{b}" in out2


def test_overlong_math_span_is_escaped() -> None:
    # >120-char "math" is a currency-dollar mispairing, not a formula.
    out, _ = _inline("$x_{1} " + "word " * 30 + "$ end")
    assert "$" not in out.replace(r"\$", "")


def test_display_glue_with_live_dollar_in_body_is_escaped() -> None:
    # $$…$$ gluing stray author dollars around real spans carries a live $
    # in its body — rejected wholesale, everything renders as literal $.
    out, _ = _inline("a $$2^{30}$ ($∼$$ b")
    assert "$" not in out.replace(r"\$", "")


def test_lint_math_spans_mirrors_the_demotion_predicates() -> None:
    # One complaint per distinct span the exporter would demote — the
    # write-path lint (`_draft_lint.math_form_hint`) delegates here so the
    # two surfaces can never disagree.
    bad_braces = latex.lint_math_spans(r"ratio $\sqrt{2/\sqrt{3}$ holds")
    assert len(bad_braces) == 1 and "unbalanced" in bad_braces[0]
    money = latex.lint_math_spans("costs $10-50 per oligomer, versus $200")
    assert len(money) == 1 and "prose/currency" in money[0]
    stray = latex.lint_math_spans("a single $ sign here")
    assert len(stray) == 1 and "stray unpaired" in stray[0]
    # clean math + escaped literal dollars: silent
    assert latex.lint_math_spans(r"Euler: $e^{i\pi} = -1$.") == []
    assert latex.lint_math_spans(r"Scaffold \$300 and staples \$200.") == []
    # the same offending span twice → one complaint (deduped)…
    twice = latex.lint_math_spans(r"$\sqrt{2$ and again $\sqrt{2$")
    assert len(twice) == 1
    # …and the dedup SKIPS the repeat rather than stopping the scan — a
    # later, distinct offender still gets its own complaint.
    mixed = latex.lint_math_spans(r"$\sqrt{2$ x $\sqrt{2$ y $\sqrt{3$")
    assert len(mixed) == 2
    # a long span is truncated in the complaint, not quoted wholesale
    long_span = "$x_{1} " + "word " * 30 + "$ end"
    (c,) = latex.lint_math_spans(long_span)
    assert "…" in c and len(c) < 200


def test_standalone_equation_detection() -> None:
    # Exactly one $$…$$ span filling the whole (stripped) chunk text.
    assert latex._standalone_equation("$$E = mc^2$$") == ("E = mc^2", False)
    # a trailing * is the starred/unnumbered escape hatch.
    assert latex._standalone_equation("  $$E = mc^2$$*  ") == ("E = mc^2", True)
    # any other text around the math disqualifies it (ordinary paragraph).
    assert latex._standalone_equation("see $$E = mc^2$$ above") is None
    assert latex._standalone_equation("$$E = mc^2$$ and $$F=ma$$") is None
    # unbalanced braces / an empty body never render as an equation env.
    assert latex._standalone_equation("$$\\sqrt{2$$") is None
    assert latex._standalone_equation("$$   $$") is None
    assert latex._standalone_equation("") is None
    assert latex._standalone_equation(None) is None


def test_table_row_leading_bracket_brace_protected() -> None:
    # A row starting with a literal "[2]" would parse as the previous row's
    # \\[…] optional vertical-space argument ("Illegal unit of measure").
    from types import SimpleNamespace

    chunk = SimpleNamespace(
        meta={
            "table": {
                "header": ["Ref", "System"],
                "rows": [["[1] Rothemund", "2D origami"], ["[2] Douglas", "3D"]],
                "caption": "",
            }
        },
        text="| Ref | System |",
    )
    ctx = _ctx("", None, None, None, None)
    lines = latex._render_table(chunk, ctx, "")
    row_lines = [ln for ln in lines if ln.startswith("{[}")]
    assert len(row_lines) == 2, lines


def test_raw_percent_and_hash_inside_math_are_escaped() -> None:
    # A raw % inside a passed-through math span starts a LaTeX comment
    # mid-math — it eats the closing $ and the rest of the source line
    # ("Missing $ inserted"; prod chunk 1507177's ``CV $<20%$``). A raw #
    # is a macro-parameter error. Both must ride escaped, math intact.
    out, _ = _inline("monodisperse (CV $<20%$) and grid $a#1$ end")
    assert r"$<20\%$" in out
    assert r"$a\#1$" in out
    assert "%" not in out.replace(r"\%", "")
    # an already-escaped \% inside math is left alone (no double escape)
    out2, _ = _inline(r"target $<5\%$ done")
    assert r"$<5\%$" in out2 and r"\\%" not in out2


def test_math_inside_inline_code_restores_no_nul_placeholder() -> None:
    # Math stashed in step 1 lands INSIDE the later-stashed \texttt span; the
    # restore must run until no \x00i\x00 placeholder remains — a leftover is
    # a literal NUL in the .tex, which LuaTeX fatals on ("invalid character").
    out, _ = _inline("`[Biotin]–[PEG$_{7nm}$]–[comp-E]`")
    assert "\x00" not in out
    # the math span survived verbatim (empty-base repair folds PEG inside)
    assert r"$\mathrm{PEG}_{7nm}$" in out


def test_italic_single_star_to_emph() -> None:
    # Single-* emphasis → \emph (parity with the web reader + docx). ** stays
    # bold (not italicised), spaced multiplication is left alone, and * inside
    # math is protected (the formula passes through verbatim).
    out, _ = _inline("a *directly bonded* pair, **not** 2 * 3, and $a*b$ math")
    assert r"\emph{directly bonded}" in out
    assert r"\textbf{not}" in out and r"\emph{not}" not in out
    assert "2 * 3" in out  # spaced multiplication untouched
    assert "$a*b$" in out  # star inside math not turned into emphasis


def test_cross_ref_and_citation() -> None:
    out, ctx = _inline("As shown in [dc41] and [§kong24~3] and paper:smith2024.")
    assert r"\cref{chunk:dc41}" in out
    assert r"\cite{kong24}" in out and r"\cite{smith2024}" in out
    assert ctx.cited == ["kong24", "smith2024"]


def test_latex_cite_command_folds_to_single_cite() -> None:
    # A draft carrying verbatim LaTeX \cite{key} must render ONE clean
    # \cite{key} — not the old escaped/doubled \textbackslash{}cite\{…\}.
    out, ctx = _inline(r"acid on the Zr nodes \cite{thiolfunctionalized20}.")
    assert r"\cite{thiolfunctionalized20}" in out
    assert r"\textbackslash{}cite" not in out  # no escaped-literal leak
    assert out.count(r"\cite{") == 1  # exactly one cite command
    assert ctx.cited == ["thiolfunctionalized20"]


def test_latex_multi_key_cite_groups() -> None:
    # \cite{a,b} folds through the one-key-per-bracket grammar but is merged
    # back into a single grouped \cite{a,b} (biblatex prints "[1, 2]").
    out, ctx = _inline(r"both \cite{nassar26, amidoximegrafted24} agree.")
    assert r"\cite{nassar26,amidoximegrafted24}" in out
    assert ctx.cited == ["nassar26", "amidoximegrafted24"]
    # Cites the author spaced apart are NOT merged.
    out2, _ = _inline(r"see \cite{kong24} and \cite{smith25} apart.")
    assert r"\cite{kong24}" in out2 and r"\cite{smith25}" in out2
    assert r"\cite{kong24,smith25}" not in out2


def test_latex_empty_base_math_gets_a_base() -> None:
    # `Zr$_6$` puts the base outside the math; fold it in so it isn't a
    # floating subscript. Multi-fragment `$W_{18}$O$_{49}$` too.
    out, _ = _inline(r"the Zr$_6$ node and UO$_2^{2+}$ ion and $W_{18}$O$_{49}$.")
    assert r"$\mathrm{Zr}_6$" in out
    assert r"$\mathrm{UO}_2^{2+}$" in out
    # adjacent repaired fragments get a {} separator — bare $$ would be a
    # display-math opener (see test_adjacent_math_spans_do_not_glue…).
    assert r"$W_{18}${}$\mathrm{O}_{49}$" in out


def test_paper_handle_renders_citation() -> None:
    # a paper handle [pc10] / [pa99] → \cite via the cite_key.
    out, ctx = _inline("see [pc10] here", store=_PaperStore())
    assert r"\cite{kong24}" in out and ctx.cited == ["kong24"]


def test_record_handle_renders_nothing() -> None:
    # A thought handle [me5] is provenance-only in export — dropped.
    out, ctx = _inline("aside [me5] here")
    assert "me5" not in out and ctx.cited == []


def test_computed_evidence_handle_renders_as_text() -> None:
    # A bracket-handle cite of a computational-evidence kind (a simulation
    # structure, [st12]) is real grounding, not a bibliography entry — it
    # renders as plain text instead of being silently dropped.
    out, ctx = _inline("as computed in [st12] here")
    assert "st12" in out
    assert ctx.cited == []  # not a bibliography \cite — no key minted


def test_computed_evidence_handle_display_form_uses_surface() -> None:
    # A display-link cite ([text](st12)) keeps the authored surface text,
    # not the raw handle.
    out, _ = _inline("see [the DFT relaxation](st12) here")
    assert "the DFT relaxation" in out
    assert "st12" not in out


def test_legacy_pilcrow_xref_maps_to_dc() -> None:
    # A legacy [¶abc123] still resolves via the base-58 → dc map.
    out, _ = _inline("see [¶abc123]", legacy_to_dc={"abc123": "dc41"}, known={"dc41"})
    assert r"\cref{chunk:dc41}" in out


def test_dangling_cross_ref_downgrades() -> None:
    # dc41 is NOT a live handle → no \cref, surface text kept, warned.
    out, ctx = _inline("see [the intro](dc41) here", known=set())
    assert r"\cref" not in out and r"\hyperref" not in out
    assert "the intro" in out
    assert any("dc41" in w for w in ctx.warnings)


def test_display_link_and_url() -> None:
    out, _ = _inline("[the intro](dc41) and [DDG](https://duckduckgo.com)")
    assert r"\hyperref[chunk:dc41]{the intro}" in out
    assert r"\href{https://duckduckgo.com}{DDG}" in out


def test_authoring_link_renders_nothing() -> None:
    out, ctx = _inline("provenance [[memory:6184]] here")
    assert "memory" not in out and "6184" not in out
    assert ctx.cited == []


def test_cyrillic_homoglyph_kept_raw_not_cyrt() -> None:
    # A Cyrillic Т (extraction homoglyph) must NOT become \CYRT — that
    # command is undefined in the LuaLaTeX preamble and fatals the compile
    # (killed the nano-computer send). A pixel-identical homoglyph maps to
    # its Latin twin (renders perfectly); a non-homoglyph Cyrillic letter
    # stays raw (at worst a missing-glyph rule).
    out, _ = _inline("wick-vapor interface Т temperature")
    assert "CYRT" not in out
    assert "interface T temperature" in out
    out2, _ = _inline("shape ж stays raw")
    assert "ж" in out2 and "cyr" not in out2.lower()


def test_unicode_translated_to_latex() -> None:
    out, _ = _inline("uptake ≈ 2.6 with α and →")
    assert "≈" not in out and "α" not in out  # non-ASCII gone
    assert r"\approx" in out and r"\alpha" in out


def test_unicode_subscripts_transliterated() -> None:
    # literal Unicode subscripts (MoS₂, CO₂) — pylatexenc keeps these
    # verbatim and pdflatex hard-errors on them; we transliterate to
    # \textsubscript so they render and the build never fails on them.
    out, _ = _inline("thin films of MoS₂ and CO₂ capture")
    assert "₂" not in out
    assert r"MoS\textsubscript{2}" in out and r"CO\textsubscript{2}" in out


def test_unicode_superscripts_and_runs_grouped() -> None:
    # superscript run 10⁻³ → one \textsuperscript{-3}; subscript run ₁₀
    # → one \textsubscript{10} (one box, not two).
    out, _ = _inline("rate 10⁻³ and index x₁₀")
    assert r"\textsuperscript{-3}" in out
    assert r"\textsubscript{10}" in out


def test_cjk_runs_wrapped_in_cjktext() -> None:
    # A Japanese / Chinese run has no LaTeX-command mapping (pylatexenc keeps
    # it verbatim) and no glyph in Latin Modern — it would render as a
    # missing-glyph rule. We wrap contiguous CJK runs in \cjktext{…} so the
    # preamble's CJK font renders them; ASCII around it is untouched.
    out, _ = _inline("the material 二酸化炭素 (CO2) and もののあはれ here")
    assert r"\cjktext{二酸化炭素}" in out
    assert r"\cjktext{もののあはれ}" in out
    # ASCII prose is not wrapped.
    assert r"\cjktext{the}" not in out


def test_wrap_cjk_noop_without_cjk() -> None:
    # No CJK → the encoder adds no \cjktext, leaving ordinary prose alone.
    assert latex._wrap_cjk("plain ascii and \\approx") == "plain ascii and \\approx"


def test_acronym_keymap_dedups_collisions() -> None:
    km = latex._acronym_keymap({"PEI": "polyethyleneimine", "P.E.I.": "place"})
    # both sanitise to "pei"; the map keeps them distinct
    assert km["PEI"] != km["P.E.I."]
    assert len(set(km.values())) == 2


def test_glsify_known_abbrev() -> None:
    out, _ = _inline("We graft PEI; PEINE differs.", {"PEI": "polyethyleneimine"})
    assert r"\gls{pei}" in out
    assert "PEINE" in out  # not a whole-word PEI


def test_glsify_plural_uses_glspl() -> None:
    """A plural surface (MOFs) links to the same term as MOF rather than
    leaving the plural bare. Here the plural is a *later* use, so it renders
    as the tooltip-wrapped short (\\glspltip), not a plain \\glspl."""
    out, _ = _inline("one MOF, several MOFs.", {"MOF": "metal-organic framework"})
    assert r"\gls{mof}" in out  # first use expands inline
    assert r"\glspltip{mof}" in out  # later plural → tooltip-wrapped short
    assert "MOFs" not in out  # the plural was absorbed, not left literal


def test_glsify_tooltip_on_later_use() -> None:
    """First use expands (plain \\gls); every later use is the bare short
    wrapped in a \\glstip pdftooltip revealing the full term on hover."""
    out, _ = _inline("Use PEI first, then PEI again.", {"PEI": "polyethyleneimine"})
    assert out.count(r"\gls{pei}") == 1  # only the first use is a plain \gls
    assert r"\glstip{pei}" in out  # the later use is tooltip-wrapped


def test_glsify_plural_no_false_match() -> None:
    """A trailing-s word that merely starts with a short is left alone."""
    out, _ = _inline("DNase activity in DNA.", {"DNA": "deoxyribonucleic acid"})
    assert r"\gls{dna}" in out
    assert "DNase" in out  # not glspl{dna}


def test_handle_xref_dc_renders_cref() -> None:
    """Universal-handle single-bracket [dc<id>] -> \\cref to the in-draft chunk."""
    ctx = latex._Ctx(keymap={}, known_handles={"dc456"})
    out = latex._render_inline("see [dc456] for detail.", ctx)
    assert r"\cref{chunk:dc456}" in out


def test_handle_paper_pc_pa_render_cite() -> None:
    """[pc<id>]/[pa<id>] resolve via the store to the paper's cite_key -> \\cite."""
    import types

    store = types.SimpleNamespace(
        resolve_handle=lambda h: (
            types.SimpleNamespace(public_id="miller23")
            if h in ("pc789", "pa123")
            else None
        )
    )
    ctx = latex._Ctx(keymap={}, known_handles=set(), store=store)
    out = latex._render_inline("per [pc789] and again [pa123].", ctx)
    assert out.count(r"\cite{miller23}") == 2
    assert ctx.cited == ["miller23"]  # collapsed to one bib entry


def test_handle_patent_pk_renders_cite() -> None:
    """[pk<id>] (a patent chunk) resolves to the patent's cite_key -> \\cite."""
    import types

    store = types.SimpleNamespace(
        resolve_handle=lambda h: (
            types.SimpleNamespace(public_id="ep1234567b1") if h == "pk55" else None
        )
    )
    ctx = latex._Ctx(keymap={}, known_handles=set(), store=store)
    out = latex._render_inline("see [pk55].", ctx)
    assert r"\cite{ep1234567b1}" in out
    assert ctx.cited == ["ep1234567b1"]


def test_handle_finding_fi_renders_cite_via_meta() -> None:
    """[fi<id>] cites its primary_cite_key once established (so it merges
    with a direct cite of that paper), else its pub_id placeholder. A
    NON-hub finding — regression test: the single-key path is unchanged
    by the Taproot hub-aware resolver (Phase 1)."""
    import types

    established = types.SimpleNamespace(meta={"primary_cite_key": "miller23"})
    inflight = types.SimpleNamespace(meta={"pub_id": "ab12c3"})
    store = _FindingStore({7: established, 9: inflight})
    ctx = latex._Ctx(keymap={}, known_handles=set(), store=store)
    out = latex._render_inline("est [fi7], inflight [fi9].", ctx)
    assert r"\cite{miller23}" in out  # established → primary cite_key
    assert r"\cite{ab12c3}" in out  # in-flight → pub_id placeholder


def test_bare_number_and_bare_pub_id_brackets_stay_literal() -> None:
    """Divergence lock (Taproot Phase 1): a bare ``[42]`` or a bare
    ``[<pub_id>]`` (base32, e.g. ``ab12c3``) written directly in draft
    prose is NOT the ``precis resolve`` placeholder grammar — the draft
    ``mentions`` grammar only recognises a handle (``[fi42]``, 2-letter
    prefix + digits), so both stay LITERAL text, never a ``\\cite{}``.
    This pins the boundary between the two citation surfaces so a future
    grammar change can't silently reconverge them (Phase 2 pins are a
    distinct, explicit ``[<pub_id>>...]`` / ``[<pub_id>+...]`` syntax)."""
    out, ctx = _inline("see [42] and [ab12c3] in the log.")
    assert "[42]" in out
    assert "[ab12c3]" in out
    assert r"\cite" not in out
    assert ctx.cited == []


# ── Taproot claim-hub finding handle (Phase 1 — living citations reach
# draft export): a [fi<id>] finding handle that resolves to a
# TAPROOT:claim hub cites its *derived* establishes originator(s)
# instead of a stored primary_cite_key. DB-backed, mirroring
# tests/test_taproot_seniority.py's setup (mint_hub / attach_evidence +
# a `cites` edge to split originators from corroborators).

_HUB_CLAIM = CanonicalClaim(
    sentence="Pd/C catalyzes Suzuki coupling at room temperature with a mild base.",
    scope={"material": "Pd/C", "method": "Suzuki coupling", "regime": "RT"},
)


def _hub_finding_handle(hub_ref_id: int) -> str:
    return _handle_registry.format_handle("finding", hub_ref_id)


def test_hub_finding_single_originator_renders_single_cite(store) -> None:
    hub = mint_hub(store, _HUB_CLAIM)
    origin = store.insert_ref(
        kind="paper", slug="latxo01", title="Original report", year=2001, meta={}
    ).id
    follow = store.insert_ref(
        kind="paper", slug="latxf05", title="Follow-up", year=2005, meta={}
    ).id
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=origin, role="corroborates")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=follow, role="corroborates")
    store.add_link(src_ref_id=follow, dst_ref_id=origin, relation="cites")

    ctx = latex._Ctx(keymap={}, known_handles=set(), store=store)
    out = latex._render_inline(f"see [{_hub_finding_handle(hub)}].", ctx)

    # Single derived originator stays on the EXACT single-cite path.
    assert r"\cite{latxo01}" in out
    assert out.count(r"\cite{") == 1
    assert ctx.cited == ["latxo01"]


def test_hub_finding_multiple_originators_renders_multi_cite(store) -> None:
    hub = mint_hub(store, _HUB_CLAIM)
    a = store.insert_ref(
        kind="paper", slug="latxa01", title="A — first report", year=2001, meta={}
    ).id
    b = store.insert_ref(
        kind="paper", slug="latxb02", title="B — second report", year=2002, meta={}
    ).id
    citer = store.insert_ref(
        kind="paper", slug="latxc09", title="Citer", year=2009, meta={}
    ).id
    for p in (a, b, citer):
        attach_evidence(store, hub_ref_id=hub, paper_ref_id=p, role="corroborates")
    store.add_link(src_ref_id=citer, dst_ref_id=a, relation="cites")
    store.add_link(src_ref_id=citer, dst_ref_id=b, relation="cites")

    ctx = latex._Ctx(keymap={}, known_handles=set(), store=store)
    out = latex._render_inline(f"see [{_hub_finding_handle(hub)}].", ctx)

    assert r"\cite{latxa01,latxb02}" in out
    assert ctx.cited == ["latxa01", "latxb02"]  # both land in the .bib


def test_hub_finding_multi_cite_gets_doi_ul_link_group(store) -> None:
    """Normal-mode cite-links: a hub cite with two DOI-bearing originator
    papers gets one small doi/UL ``\\href`` pair per distinct paper, right
    after the combined ``\\cite{...}`` — and none at all with both
    ``doi_links=False`` and ``library_links=False``."""
    hub = mint_hub(store, _HUB_CLAIM)
    a = store.insert_ref(
        kind="paper", slug="latxa11", title="A — first report", year=2001, meta={}
    ).id
    b = store.insert_ref(
        kind="paper", slug="latxb12", title="B — second report", year=2002, meta={}
    ).id
    citer = store.insert_ref(
        kind="paper", slug="latxc19", title="Citer", year=2009, meta={}
    ).id
    for p in (a, b, citer):
        attach_evidence(store, hub_ref_id=hub, paper_ref_id=p, role="corroborates")
    store.add_link(src_ref_id=citer, dst_ref_id=a, relation="cites")
    store.add_link(src_ref_id=citer, dst_ref_id=b, relation="cites")
    store.insert_ref_identifiers(a, [("doi", "10.1/latxa11", "manual")])
    store.insert_ref_identifiers(b, [("doi", "10.1/latxb12", "manual")])

    ctx = latex._Ctx(keymap={}, known_handles=set(), store=store)
    out = latex._render_inline(f"see [{_hub_finding_handle(hub)}].", ctx)

    assert r"\cite{latxa11,latxb12}" in out
    assert out.count(r"\href{https://doi.org/10.1/latxa11}{doi}") == 1
    assert out.count(r"\href{https://doi.org/10.1/latxb12}{doi}") == 1
    assert out.count("uol.primo.exlibrisgroup.com") == 2  # one UL link/paper

    ctx_off = latex._Ctx(
        keymap={},
        known_handles=set(),
        store=store,
        doi_links=False,
        library_links=False,
    )
    out_off = latex._render_inline(f"see [{_hub_finding_handle(hub)}].", ctx_off)
    assert r"\cite{latxa11,latxb12}" in out_off
    assert r"\href" not in out_off


def test_cite_link_doi_and_library_switches_are_independent(store) -> None:
    """``doi_links`` and ``library_links`` are independent switches: doi-on/
    library-off emits only the ``doi`` run; doi-off/library-on emits only
    the library-search run."""
    a = store.insert_ref(
        kind="paper", slug="latxdoi1", title="A", year=2001, meta={}
    ).id
    store.insert_ref_identifiers(a, [("doi", "10.1/latxdoi1", "manual")])

    ctx_doi_only = latex._Ctx(
        keymap={},
        known_handles=set(),
        store=store,
        doi_links=True,
        library_links=False,
    )
    out = latex._render_inline("see [§latxdoi1].", ctx_doi_only)
    assert r"\href{https://doi.org/10.1/latxdoi1}{doi}" in out
    assert "uol.primo.exlibrisgroup.com" not in out

    ctx_library_only = latex._Ctx(
        keymap={},
        known_handles=set(),
        store=store,
        doi_links=False,
        library_links=True,
    )
    out = latex._render_inline("see [§latxdoi1].", ctx_library_only)
    assert r"\href{https://doi.org/10.1/latxdoi1}{doi}" not in out
    assert "uol.primo.exlibrisgroup.com" in out


def test_hub_finding_corroborator_only_fallback(store) -> None:
    # No intra-supporter `cites` edge held -> no derived originator; the
    # living citation falls back to the corroborator(s) rather than
    # going in-flight.
    hub = mint_hub(store, _HUB_CLAIM)
    only = store.insert_ref(
        kind="paper", slug="latxd01", title="Sole supporter", year=2001, meta={}
    ).id
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=only, role="corroborates")

    ctx = latex._Ctx(keymap={}, known_handles=set(), store=store)
    out = latex._render_inline(f"see [{_hub_finding_handle(hub)}].", ctx)

    assert r"\cite{latxd01}" in out
    assert ctx.cited == ["latxd01"]


def test_hub_finding_no_evidence_renders_no_cite(store) -> None:
    hub = mint_hub(store, _HUB_CLAIM)

    ctx = latex._Ctx(keymap={}, known_handles=set(), store=store)
    out = latex._render_inline(f"pending [{_hub_finding_handle(hub)}] evidence.", ctx)

    # In-flight — no resolvable evidence yet: no cite, no dangling command.
    assert r"\cite" not in out
    assert ctx.cited == []


# ── Taproot Phase 2 — authorial pins reach draft export ────────────────
# `[fi<id>>pa5]` (replace) / `[fi<id>+pa5]` (supplement) — the shared
# `precis.taproot.cite.apply_pin` policy applied through the `mentions`
# grammar's optional `pin` capture group.


def _hub_with_derived_originator(store, *, origin_key: str, follow_key: str) -> int:
    hub = mint_hub(store, _HUB_CLAIM)
    origin = store.insert_ref(
        kind="paper", slug=origin_key, title="Original report", year=2001, meta={}
    ).id
    follow = store.insert_ref(
        kind="paper", slug=follow_key, title="Follow-up", year=2005, meta={}
    ).id
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=origin, role="corroborates")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=follow, role="corroborates")
    store.add_link(src_ref_id=follow, dst_ref_id=origin, relation="cites")
    return hub


def test_pin_replace_cites_pinned_not_derived_originator(store) -> None:
    hub = _hub_with_derived_originator(
        store, origin_key="latxp01", follow_key="latxq01"
    )
    pinned = store.insert_ref(
        kind="paper", slug="latxr01", title="Author's pick", meta={}
    ).id
    handle = _handle_registry.format_handle("paper", pinned)

    ctx = latex._Ctx(keymap={}, known_handles=set(), store=store)
    out = latex._render_inline(f"see [{_hub_finding_handle(hub)}>{handle}].", ctx)

    assert r"\cite{latxr01}" in out
    assert "latxp01" not in out
    assert ctx.cited == ["latxr01"]
    # Replace-pin diverging from the derived originator adds a warning.
    assert any("reconsider" in w for w in ctx.warnings)


def test_pin_supplement_adds_to_derived_originators(store) -> None:
    hub = _hub_with_derived_originator(
        store, origin_key="latxp02", follow_key="latxq02"
    )
    pinned = store.insert_ref(
        kind="paper", slug="latxr02", title="Extra evidence", meta={}
    ).id
    handle = _handle_registry.format_handle("paper", pinned)

    ctx = latex._Ctx(keymap={}, known_handles=set(), store=store)
    out = latex._render_inline(f"see [{_hub_finding_handle(hub)}+{handle}].", ctx)

    assert r"\cite{latxp02,latxr02}" in out
    assert ctx.cited == ["latxp02", "latxr02"]
    # Supplement never diverges — no advisory.
    assert not any("reconsider" in w for w in ctx.warnings)


def test_pin_on_non_hub_finding_ignored_with_warning() -> None:
    import types

    established = types.SimpleNamespace(meta={"primary_cite_key": "latxn01"})
    store = _FindingStore({7: established})
    ctx = latex._Ctx(keymap={}, known_handles=set(), store=store)

    out = latex._render_inline("see [fi7>pa5].", ctx)

    # Unchanged — the pin is meaningless on a non-hub finding.
    assert r"\cite{latxn01}" in out
    assert any("pin on fi7 ignored" in w for w in ctx.warnings)


def test_pin_passage_handle_resolves_to_parent_paper(store) -> None:
    hub = _hub_with_derived_originator(
        store, origin_key="latxp03", follow_key="latxq03"
    )
    pinned = store.insert_ref(
        kind="paper", slug="latxr03", title="Grounded passage source", meta={}
    ).id
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, 0, 'paragraph', 'a passage') RETURNING chunk_id",
            (pinned,),
        ).fetchone()
        conn.commit()
    assert row is not None
    chunk_handle = f"pc{int(row[0])}"

    ctx = latex._Ctx(keymap={}, known_handles=set(), store=store)
    out = latex._render_inline(f"see [{_hub_finding_handle(hub)}>{chunk_handle}].", ctx)

    assert r"\cite{latxr03}" in out
    assert ctx.cited == ["latxr03"]


# ── reMarkable send-to-tablet footnote mode (footnote_refs=True) ──────


def _fn_ctx(store):
    """A footnote-mode render context (reMarkable send-to-tablet)."""
    return latex._Ctx(keymap={}, known_handles=set(), store=store, footnote_refs=True)


def test_footnote_mode_pc_handle_quotes_the_chunk() -> None:
    """A chunk-addressed [pc<id>] becomes a self-contained \\footnote: the
    human cite + \\cite (bibliography number) + the referenced excerpt."""
    import types

    store = types.SimpleNamespace(
        resolve_handle=lambda h: (
            types.SimpleNamespace(public_id="kong24") if h == "pc10" else None
        ),
        universal_chunk=lambda h: (
            {"text": "MOF-808 adsorbs uranyl at 2.6 mmol/g in seawater."}
            if h == "pc10"
            else None
        ),
        get_ref=lambda kind, id: (
            types.SimpleNamespace(
                id=99,
                title="Uranium capture",
                slug="kong24",
                meta={"authors": [{"name": "Kong, L."}], "year": 2024},
            )
            if (kind, id) == ("paper", "kong24")
            else None
        ),
    )
    store.drafts = store  # sub-store facade shim
    out = latex._render_inline("As in [pc10].", (ctx := _fn_ctx(store)))
    assert r"\footnote{" in out  # a footnote, not an inline \cite
    assert r"\cite{kong24}" in out  # prints [N] + registers the bib entry
    assert "Kong et al., 2024" in out  # the human cite line
    assert "MOF-808 adsorbs uranyl" in out  # the quoted chunk excerpt
    assert ctx.cited == ["kong24"]  # still assembled into the end bibliography


def test_footnote_mode_record_handle_has_no_excerpt() -> None:
    """A bare record handle [pa<id>] (no specific chunk) footnotes the cite
    line + number but quotes nothing (no \\emph excerpt block)."""
    import types

    store = types.SimpleNamespace(
        resolve_handle=lambda h: (
            types.SimpleNamespace(public_id="miller23") if h == "pa123" else None
        ),
        universal_chunk=lambda h: None,
        get_ref=lambda kind, id: types.SimpleNamespace(
            id=1, title="T", slug="miller23", meta={"authors": [{"name": "Miller, A."}]}
        ),
    )
    store.drafts = store  # sub-store facade shim
    out = latex._render_inline("see [pa123].", _fn_ctx(store))
    assert r"\footnote{" in out and r"\cite{miller23}" in out
    assert r"\emph{" not in out  # no excerpt block for a chunk-less ref


def test_footnote_mode_slug_cite_resolves_chunk_excerpt() -> None:
    """The classic [§slug~n] cite footnotes and quotes the paper chunk at
    ordinal n (via the new chunk_text_at store helper)."""
    import types

    store = types.SimpleNamespace(
        get_ref=lambda kind, id: (
            types.SimpleNamespace(
                id=42,
                title="Amidoxime study",
                slug="smith2024",
                meta={
                    "authors": [{"name": "Smith, J."}],
                    "publication_date": "2024-05",
                },
            )
            if (kind, id) == ("paper", "smith2024")
            else None
        ),
        chunk_text_at=lambda ref_id, ordn: (
            "Amidoxime groups bind uranyl selectively over vanadium."
            if (ref_id, ordn) == (42, 3)
            else None
        ),
    )
    store.drafts = store  # sub-store facade shim
    out = latex._render_inline("prior work [§smith2024~3].", (ctx := _fn_ctx(store)))
    assert r"\footnote{" in out and r"\cite{smith2024}" in out
    assert "Amidoxime groups bind uranyl" in out
    assert ctx.cited == ["smith2024"]


def test_footnote_mode_patent_uses_patent_citation_string() -> None:
    """_source_footnote renders a patent's in-text citation string as the
    footnote's human-readable line."""
    import types

    store = types.SimpleNamespace(
        get_ref=lambda kind, id: (
            types.SimpleNamespace(
                id=7,
                title="Catalyst",
                slug="us2943737",
                meta={"country": "us", "doc_number": "2943737", "kind_code": "A"},
            )
            if (kind, id) == ("patent", "us2943737")
            else None
        ),
    )
    out = latex._source_footnote("us2943737", "patent", "", _fn_ctx(store))
    assert r"\footnote{" in out and r"\cite{us2943737}" in out
    assert "U.S. Patent No. 2,943,737" in out


def test_footnote_excerpt_trims_to_a_boundary() -> None:
    """A long chunk is trimmed to ~one sentence and ellipsised, not dumped
    whole, so a footnote stays readable on the tablet."""
    ctx = latex._Ctx(keymap={}, known_handles=set())
    long_text = "alpha beta gamma delta " * 40  # ~920 chars, no sentence stops
    out = latex._footnote_excerpt(long_text, ctx)
    assert out.startswith(r"\emph{") and out.endswith("}")
    # the trailing "…" is unicode-encoded to \textellipsis by the prose pass
    assert r"\textellipsis" in out and len(out) < len(long_text)


def test_footnote_mode_hub_renders_rich_claim_footnote(store) -> None:
    """A [fi<id>] claim-hub cite in reMarkable mode becomes ONE
    self-contained footnote: the claim sentence, the publish ladder with
    the current rung bolded (no publish row = candidate), each supporting
    citation's grounding pc handle + the paper title in bold + its
    bibliography number + the FULL grounding passage text, and the papers
    registered for the end bibliography."""
    hub = mint_hub(store, _HUB_CLAIM)
    origin = store.insert_ref(
        kind="paper", slug="latxh01", title="Original report", year=2001, meta={}
    ).id
    follow = store.insert_ref(
        kind="paper", slug="latxh02", title="Follow-up study", year=2005, meta={}
    ).id
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) VALUES "
            "(%s, 0, 'paragraph', 'Coupling proceeds at room temperature.') "
            "RETURNING chunk_id",
            (origin,),
        ).fetchone()
        conn.commit()
    assert row is not None
    origin_pc = f"pc{int(row[0])}"
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=origin,
        role="corroborates",
        meta={"source_handle": origin_pc},
    )
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=follow,
        role="corroborates",
        meta={"source_handle": "pc9002"},
    )
    store.add_link(src_ref_id=follow, dst_ref_id=origin, relation="cites")

    ctx = _fn_ctx(store)
    out = latex._render_inline(f"see [{_hub_finding_handle(hub)}].", ctx)

    assert out.count(r"\footnote{") == 1  # one rich footnote, not one per key
    assert f"Claim fi{hub}" in out
    assert r"\textbf{candidate} $\to$ reviewed $\to$ signed" in out
    assert "Pd/C catalyzes Suzuki coupling" in out  # the nanopub statement
    assert r"\textbf{Original report}" in out and "(2001)" in out
    assert r"\textbf{Follow-up study}" in out and "(2005)" in out
    # gr345703: internal chunk handles never reach a reader-facing export —
    # the passage is labelled by ordinal, not by ``pc<id>``.
    assert origin_pc not in out and "pc9002" not in out
    # The held grounding chunk's FULL passage is quoted under its label;
    # a handle this host has no chunk for quotes nothing (no empty shell).
    assert "\\emph{excerpt: ``Coupling proceeds at room temperature.''}" in out
    assert "excerpt: ``''" not in out
    assert r"\cite{latxh01}" in out and r"\cite{latxh02}" in out
    assert set(ctx.cited) == {"latxh01", "latxh02"}


def test_footnote_mode_hub_bolds_reviewed_rung_and_frozen_statement(store) -> None:
    """An approved hub's footnote bolds the reviewed rung and quotes the
    FROZEN approved title (what a signature would cover), not the live hub
    wording."""
    hub = _hub_with_derived_originator(
        store, origin_key="latxh03", follow_key="latxh04"
    )
    row = store.nanopub_create_publish_row(hub)
    assert store.nanopub_approve(
        row.id,
        approved_title="Frozen approved claim sentence.",
        claim_sha="0" * 64,
        aida_uri="http://purl.org/aida/x",
        grounding={},
    )

    out = latex._render_inline(f"see [{_hub_finding_handle(hub)}].", _fn_ctx(store))

    assert r"candidate $\to$ \textbf{reviewed} $\to$ signed" in out
    assert "Frozen approved claim sentence." in out


def test_footnote_mode_hub_lists_dispute_and_citation_miss(store) -> None:
    """Validation issues on record — a contradicts edge and a
    meta.citation_misses entry — surface in the footnote's Issues run,
    the disputing paper's title in bold."""
    hub = _hub_with_derived_originator(
        store, origin_key="latxh05", follow_key="latxh06"
    )
    disputer = store.insert_ref(
        kind="paper", slug="latxh07", title="Contrary evidence", year=2010, meta={}
    ).id
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=disputer, role="contradicts")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
            ('{"citation_misses": [{"marker": 126, "cited_ref": 5}]}', hub),
        )
        conn.commit()

    out = latex._render_inline(f"see [{_hub_finding_handle(hub)}].", _fn_ctx(store))

    assert r"\emph{Issues:}" in out
    assert r"disputed by \textbf{Contrary evidence}" in out
    assert "citation miss" in out and "126" in out


def test_footnote_mode_hub_trust_marked_once_inside_footnote(store) -> None:
    """A non-clean hub's trust text appears exactly once — on the footnote's
    Issues line — with NO duplicate inline mark trailing the footnote (the
    non-footnote path's \\textsuperscript chrome). Non-clean forced via the
    hub-harden rule: every grounding paper declared unacquirable."""
    hub = _hub_with_derived_originator(
        store, origin_key="latxh08", follow_key="latxh09"
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id IN "
            "(SELECT ref_id FROM refs WHERE kind='paper' "
            " AND ref_id IN (SELECT src_ref_id FROM links WHERE dst_ref_id=%s))",
            ('{"unacquirable_override": {"note": "paywalled", "by": "t"}}', hub),
        )
        conn.commit()

    out = latex._render_inline(f"see [{_hub_finding_handle(hub)}].", _fn_ctx(store))

    assert out.count("unverified") == 1  # Issues line only
    assert r"\textsuperscript{?}" not in out  # no trailing inline mark


# ── quote-contiguity label (docs/backlog/nanopub-quote-contiguity.md) ──


def _two_chunk_hub(
    store, *, ord2: int, mid_text: str | None = None
) -> tuple[int, int, str, str]:
    """A hub grounded by one paper at two passages: chunk1 at ord 0,
    chunk2 at ``ord2``. Returns ``(hub, paper, pc1, pc2)``."""
    hub = mint_hub(store, _HUB_CLAIM)
    paper = store.insert_ref(
        kind="paper", slug="latxg01", title="Two-passage source", year=2003, meta={}
    ).id
    with store.pool.connection() as conn:
        row1 = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) VALUES "
            "(%s, 0, 'paragraph', 'First passage text.') RETURNING chunk_id",
            (paper,),
        ).fetchone()
        if mid_text is not None:
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) VALUES "
                "(%s, %s, 'paragraph', %s)",
                (paper, ord2 - 1, mid_text),
            )
        row2 = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) VALUES "
            "(%s, %s, 'paragraph', 'Second passage text.') RETURNING chunk_id",
            (paper, ord2),
        ).fetchone()
        conn.commit()
    assert row1 is not None and row2 is not None
    pc1, pc2 = f"pc{int(row1[0])}", f"pc{int(row2[0])}"
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="corroborates",
        meta={"source_handle": pc1},
    )
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="corroborates",
        meta={"source_handle": pc2},
    )
    return hub, paper, pc1, pc2


def test_footnote_adjacent_quotes_label_contiguous_live_fallback(store) -> None:
    """Live-fallback path: no publish row, so the label is computed by
    :func:`precis.nanopub.evidence.passages_contiguous` off the live chunk
    ordering. ord 0 and ord 1, nothing between — adjacent."""
    hub, _paper, pc1, pc2 = _two_chunk_hub(store, ord2=1)
    assert store.nanopub_publish_row(hub) is None

    out = latex._render_inline(f"see [{_hub_finding_handle(hub)}].", _fn_ctx(store))

    assert f"{pc1}" not in out and f"{pc2}" not in out
    assert "excerpt 1: ``First passage text.''" in out
    assert "excerpt 2: ``Second passage text.''" in out
    assert "(contiguous excerpt)" in out
    assert "(non-contiguous excerpts)" not in out


def test_footnote_non_adjacent_quotes_label_non_contiguous_live_fallback(
    store,
) -> None:
    """A live intervening chunk between the two quoted ones breaks
    adjacency — live-fallback path (no publish row)."""
    hub, _paper, pc1, pc2 = _two_chunk_hub(
        store, ord2=2, mid_text="An unrelated middle passage."
    )
    assert store.nanopub_publish_row(hub) is None

    out = latex._render_inline(f"see [{_hub_finding_handle(hub)}].", _fn_ctx(store))

    assert f"{pc1}" not in out and f"{pc2}" not in out
    assert "(non-contiguous excerpts)" in out
    assert "(contiguous excerpt)" not in out


def test_footnote_excerpts_read_in_paper_order_with_doi_and_deep_links(store) -> None:
    """gr345703: passages are numbered in READING order (chunk ``ord``
    ascending — the prod footnote printed pc35909 before pc35908), the
    paper line carries its DOI URL, and each frozen passage gets a
    text-fragment deep link built from the signed ``searchSnip`` — the
    locators a third party can act on. Chunk handles stay out entirely."""
    hub, paper, pc1, pc2 = _two_chunk_hub(store, ord2=1)
    store.set_ref_identifier(paper, "doi", "10.1021/nn303526r")
    # Attach order is pc1 then pc2 (ord 0, 1); reverse the edge order the
    # footnote sees by re-attaching pc2 first on a fresh hub would need a
    # second fixture — instead assert on the rendered order directly.
    chunk1_id, chunk2_id = int(pc1[2:]), int(pc2[2:])
    row = store.nanopub_create_publish_row(hub)
    assert store.nanopub_approve(
        row.id,
        approved_title="Frozen claim sentence.",
        claim_sha="0" * 64,
        aida_uri="http://purl.org/aida/y",
        grounding={
            "passages": [
                {"chunk_id": chunk2_id, "snip": "second-passage-text"},
                {"chunk_id": chunk1_id, "snip": "first-passage-text"},
            ]
        },
    )

    out = latex._render_inline(f"see [{_hub_finding_handle(hub)}].", _fn_ctx(store))

    assert pc1 not in out and pc2 not in out
    first = out.index("excerpt 1: ``First passage text.''")
    second = out.index("excerpt 2: ``Second passage text.''")
    assert first < second
    assert "\\href{https://doi.org/10.1021/nn303526r}" in out
    assert (
        "\\href{https://doi.org/10.1021/nn303526r\\#:\\string~:text=first-passage-text}"
        in out
    )
    assert (
        "\\href{https://doi.org/10.1021/nn303526r\\#:\\string~:text=second-passage-text}"
        in out
    )


def test_footnote_single_quote_carries_no_contiguity_label(store) -> None:
    hub = mint_hub(store, _HUB_CLAIM)
    paper = store.insert_ref(
        kind="paper",
        slug="latxh10",
        title="Single-passage source",
        year=2004,
        meta={},
    ).id
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) VALUES "
            "(%s, 0, 'paragraph', 'Only passage text.') RETURNING chunk_id",
            (paper,),
        ).fetchone()
        conn.commit()
    assert row is not None
    pc = f"pc{int(row[0])}"
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="corroborates",
        meta={"source_handle": pc},
    )

    out = latex._render_inline(f"see [{_hub_finding_handle(hub)}].", _fn_ctx(store))

    assert pc not in out and "excerpt: ``Only passage text.''" in out
    assert "contiguous excerpt" not in out  # neither the singular nor plural form


def test_footnote_prefers_frozen_contiguity_flag_over_live_recompute(store) -> None:
    """Frozen-payload path: the hub's live ``nanopub_publish`` row carries a
    per-passage ``contiguous_group`` flag — the exact chunk_ids a reviewer
    approved. Adjacent live chunks would recompute True; the frozen payload
    says False (the paper as it was quoted at approval, immune to a later
    re-chunk), and that is what the footnote must render."""
    hub, _paper, pc1, pc2 = _two_chunk_hub(store, ord2=1)  # live-adjacent
    chunk1_id, chunk2_id = int(pc1[2:]), int(pc2[2:])
    row = store.nanopub_create_publish_row(hub)
    assert store.nanopub_approve(
        row.id,
        approved_title="Frozen claim sentence.",
        claim_sha="0" * 64,
        aida_uri="http://purl.org/aida/y",
        grounding={
            "passages": [
                {"chunk_id": chunk1_id, "contiguous_group": False},
                {"chunk_id": chunk2_id, "contiguous_group": False},
            ]
        },
    )

    out = latex._render_inline(f"see [{_hub_finding_handle(hub)}].", _fn_ctx(store))

    assert "(non-contiguous excerpts)" in out
    assert "(contiguous excerpt)" not in out


# ── paper-context sentence (docs/backlog/paper-context-sentence.md) ────


def _single_quote_hub(
    store, *, slug: str, title: str, year: int, meta: dict
) -> tuple[int, int]:
    """A hub grounded by one paper at one passage. Returns ``(hub, paper)``."""
    hub = mint_hub(store, _HUB_CLAIM)
    paper = store.insert_ref(
        kind="paper", slug=slug, title=title, year=year, meta=meta
    ).id
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) VALUES "
            "(%s, 0, 'paragraph', 'Only passage text.') RETURNING chunk_id",
            (paper,),
        ).fetchone()
        conn.commit()
    assert row is not None
    chunk_id = int(row[0])
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="corroborates",
        meta={"source_handle": f"pc{chunk_id}"},
    )
    return hub, paper


def test_footnote_renders_context_line_from_live_refs_meta(store) -> None:
    hub, _paper = _single_quote_hub(
        store,
        slug="latxj01",
        title="Context-sentenced source",
        year=2005,
        meta={
            "context_sentence": (
                "Computational study; DFT-calculated, no wet-lab work."
            )
        },
    )

    out = latex._render_inline(f"see [{_hub_finding_handle(hub)}].", _fn_ctx(store))

    assert "Context: Computational study; DFT-calculated, no wet-lab work." in out


def test_footnote_omits_context_line_when_source_carries_none(store) -> None:
    hub, _paper = _single_quote_hub(
        store, slug="latxj02", title="No sentence source", year=2006, meta={}
    )

    out = latex._render_inline(f"see [{_hub_finding_handle(hub)}].", _fn_ctx(store))

    assert "Context:" not in out


def test_footnote_context_sentence_is_latex_escaped(store) -> None:
    hub, _paper = _single_quote_hub(
        store,
        slug="latxj03",
        title="Escaped-sentence source",
        year=2007,
        meta={"context_sentence": "Yield 80% & purity_check # 3 failed."},
    )

    out = latex._render_inline(f"see [{_hub_finding_handle(hub)}].", _fn_ctx(store))

    assert r"Yield 80\% \& purity\_check \# 3 failed." in out
    # The raw unescaped specials must never reach the .tex output.
    assert "80% &" not in out


def test_footnote_prefers_frozen_context_sentence_over_live_meta(store) -> None:
    hub, paper = _single_quote_hub(
        store,
        slug="latxj04",
        title="Frozen-vs-live source",
        year=2008,
        meta={"context_sentence": "Live sentence, should not render."},
    )
    with store.pool.connection() as conn:
        (chunk_id,) = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s", (paper,)
        ).fetchone()
    row = store.nanopub_create_publish_row(hub)
    assert store.nanopub_approve(
        row.id,
        approved_title="Frozen claim sentence.",
        claim_sha="0" * 64,
        aida_uri="http://purl.org/aida/latxctxfrozen",
        grounding={
            "passages": [
                {"chunk_id": int(chunk_id), "context_sentence": "Frozen sentence wins."}
            ]
        },
    )

    out = latex._render_inline(f"see [{_hub_finding_handle(hub)}].", _fn_ctx(store))

    assert "Context: Frozen sentence wins." in out
    assert "Live sentence, should not render." not in out


def test_assemble_document_injects_remarkable_geometry() -> None:
    """remarkable=True stamps the RM2 page geometry after the preamble;
    the default export leaves the standard 1in margins untouched."""
    rm = latex.assemble_document(
        title="T", author_block=r"\author{x}", body="hi", acronyms="", remarkable=True
    )
    assert "paperwidth=157.6mm" in rm and r"\linespread" in rm
    plain = latex.assemble_document(
        title="T", author_block=r"\author{x}", body="hi", acronyms=""
    )
    assert "paperwidth=157.6mm" not in plain


def test_assemble_document_default_bib_style_is_byte_identical() -> None:
    """No ``bib_style`` → the checked-in preamble verbatim (numeric-comp)."""
    out = latex.assemble_document(
        title="T", author_block=r"\author{x}", body="hi", acronyms=""
    )
    assert latex._template_text("preamble.tex").rstrip() in out
    assert "style=numeric-comp,sorting=none" in out


def test_assemble_document_bib_style_substitutes_only_the_style_token() -> None:
    out = latex.assemble_document(
        title="T",
        author_block=r"\author{x}",
        body="hi",
        acronyms="",
        bib_style="chem-rsc",
    )
    assert r"\usepackage[backend=biber,style=chem-rsc,sorting=none]{biblatex}" in out
    assert "numeric-comp" not in out


def test_assemble_document_unvetted_bib_style_never_reaches_latex() -> None:
    out = latex.assemble_document(
        title="T",
        author_block=r"\author{x}",
        body="hi",
        acronyms="",
        bib_style="x]{evil}\\input{/etc/passwd}",
    )
    assert "evil" not in out and "style=numeric-comp" in out


@pytest.mark.parametrize(
    ("asked", "canonical"),
    [
        (None, "numeric-comp"),
        ("", "numeric-comp"),
        ("  ", "numeric-comp"),
        ("numeric-comp", "numeric-comp"),
        ("chem-rsc", "chem-rsc"),
        ("rsc", "chem-rsc"),
        ("RSC", "chem-rsc"),
        ("chem-acs", "chem-acs"),
        ("acs", "chem-acs"),
        ("nature", "nature"),
    ],
)
def test_resolve_bib_style_allowlist(asked, canonical) -> None:
    assert latex.resolve_bib_style(asked) == (canonical, None)


def test_resolve_bib_style_unknown_warns_and_keeps_default() -> None:
    style, warning = latex.resolve_bib_style("ieee")
    assert style == "numeric-comp"
    assert warning is not None
    assert "'ieee'" in warning and "not supported" in warning
    assert "chem-rsc" in warning  # lists what is supported


class _BibStore:
    """Minimal store for :func:`latex.build_bib`: resolves a slug to a
    paper / patent / datasheet ref and carries no DOI/arXiv aliases."""

    def __init__(self, refs):
        self._refs = refs  # (kind, slug) -> Ref-ish

    def get_ref(self, *, kind, id):
        return self._refs.get((kind, id))

    def identifiers_for_refs(self, ref_ids):
        return {}


def _bibref(rid, slug, kind, *, title, authors=None, year=None, meta=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        id=rid,
        slug=slug,
        kind=kind,
        title=title,
        authors=authors,
        year=year,
        meta=meta,
    )


def test_build_bib_escapes_garbage_author_names() -> None:
    # Real corpus metadata (refs 258/3023/893/933): a trailing backslash in
    # an author name rides raw into the .bib, biber copies it into the
    # .bbl, and \} there is an ESCAPED brace — the \name group never closes
    # and runaways to the next blank line ("Paragraph ended before \name
    # was complete"), killing the whole compile.
    store = _BibStore(
        {
            ("paper", "methods23"): _bibref(
                258,
                "methods23",
                "paper",
                title="Diffusion methods",
                authors=[
                    {"name": "DIFFUSION MODELS\\"},
                    {"name": "J. H. & Silvera"},
                    {"name": "IOP Publishing #3"},
                    # thin space (U+2009): would encode to \, — biber's
                    # name parser mangles that into suffix={Matthew\}
                    {"name": "Ryder, Matthew R."},
                ],
                year=2023,
            )
        }
    )
    warnings: list[str] = []
    bib = latex.build_bib(store, ["methods23"], warnings)
    assert "\\textbackslash{}" in bib  # the trailing \ is a literal glyph
    assert r"\&" in bib and r"\#" in bib
    assert "Ryder, Matthew R." in bib  # thin space → plain space, never \,
    # every brace in the entry balances (no group left open for the .bbl)
    stripped = bib.replace(r"\{", "").replace(r"\}", "")
    assert stripped.count("{") == stripped.count("}")


def test_build_bib_emits_datasheet_entry_not_stub() -> None:
    """A cited datasheet resolves to a real ``@manual`` bib entry (gr52396) —
    not the 'missing source' auto-stub — so the bibliography lists it."""
    store = _BibStore(
        {
            ("datasheet", "stm32f4"): _bibref(
                7,
                "stm32f4",
                "datasheet",
                title="STM32F4 Reference Manual",
                authors=[{"name": "STMicroelectronics"}],
                year=2019,
            )
        }
    )
    warnings: list[str] = []
    bib = latex.build_bib(store, ["stm32f4"], warnings)
    assert "@manual{stm32f4," in bib
    assert "STM32F4 Reference Manual" in bib
    assert "author = {STMicroelectronics}" in bib
    assert "howpublished = {Datasheet}" in bib
    assert "missing source" not in bib
    assert warnings == []


def test_build_bib_datasheet_carries_vendor_subtype_and_part() -> None:
    """vendor → @manual organization, subtype → howpublished label, and the
    documented part → a note (the datasheet meta fields the reader edits)."""
    store = _BibStore(
        {
            ("datasheet", "esp32c3"): _bibref(
                9,
                "esp32c3",
                "datasheet",
                title="ESP32-C3 App Note",
                year=2022,
                meta={
                    "vendor": "Espressif Systems",
                    "subtype": "app-note",
                    "part_lcsc": "C2934569",
                },
            )
        }
    )
    warnings: list[str] = []
    bib = latex.build_bib(store, ["esp32c3"], warnings)
    assert "@manual{esp32c3," in bib
    assert "organization = {Espressif Systems}" in bib
    assert "howpublished = {Application note}" in bib
    assert "note = {Part C2934569}" in bib
    assert warnings == []


def test_build_bib_article_emits_journaltitle_from_meta_journal() -> None:
    """An @article with no meta.venue falls back to meta.journal — the
    'In: (2026)' empty-venue defect (ref 450227, Surface Science)."""
    store = _BibStore(
        {
            ("paper", "azobenzene"): _bibref(
                450227,
                "azobenzene",
                "paper",
                title="Isomerization of Azobenzene Derivatives",
                year=2026,
                meta={"journal": "Surface Science"},
            )
        }
    )
    warnings: list[str] = []
    bib = latex.build_bib(store, ["azobenzene"], warnings)
    assert "journaltitle = {Surface Science}" in bib


def test_build_bib_article_venue_wins_over_journal() -> None:
    store = _BibStore(
        {
            ("paper", "p1"): _bibref(
                1,
                "p1",
                "paper",
                title="T",
                year=2020,
                meta={"venue": "Venue Name", "journal": "Journal Name"},
            )
        }
    )
    bib = latex.build_bib(store, ["p1"], [])
    assert "journaltitle = {Venue Name}" in bib
    assert "Journal Name" not in bib


def test_build_bib_article_no_journal_omits_field() -> None:
    """No venue/journal/container_title in meta → no journaltitle field at
    all (not an empty one) — the exact scenario that used to render as a
    bare 'In: (2026)' in the compiled PDF."""
    store = _BibStore(
        {("paper", "p1"): _bibref(1, "p1", "paper", title="T", year=2020, meta=None)}
    )
    bib = latex.build_bib(store, ["p1"], [])
    assert "journaltitle" not in bib


def test_build_bib_article_journal_ampersand_escaped() -> None:
    store = _BibStore(
        {
            ("paper", "p1"): _bibref(
                1,
                "p1",
                "paper",
                title="T",
                year=2020,
                meta={"journal": "Chemistry & Physics"},
            )
        }
    )
    bib = latex.build_bib(store, ["p1"], [])
    assert "journaltitle = {Chemistry \\& Physics}" in bib


def test_build_bib_article_passes_through_volume_number_pages() -> None:
    """volume/number/pages have no corpus source today (0/44485 papers) but
    the field should pass through unmodified once an enricher fills them —
    forward-compatible plumbing, not a derivation."""
    store = _BibStore(
        {
            ("paper", "p1"): _bibref(
                1,
                "p1",
                "paper",
                title="T",
                year=2020,
                meta={
                    "journal": "Surface Science",
                    "volume": "42",
                    "number": "3",
                    "pages": "100-110",
                },
            )
        }
    )
    bib = latex.build_bib(store, ["p1"], [])
    assert "volume = {42}" in bib
    assert "number = {3}" in bib
    assert "pages = {100-110}" in bib


def test_build_bib_unresolved_slug_stubs_with_warning() -> None:
    """A slug that matches no paper/patent/datasheet still degrades to a
    compile-safe stub + a warning."""
    warnings: list[str] = []
    bib = latex.build_bib(_BibStore({}), ["ghost"], warnings)
    assert "@misc{ghost," in bib and "[missing source ghost]" in bib
    assert any("ghost" in w for w in warnings)


def test_build_acronyms() -> None:
    tex = latex.build_acronyms({"PEI": "polyethyleneimine", "MOF": "metal-organic"})
    assert r"\newacronym{pei}{PEI}{polyethyleneimine}" in tex
    assert r"\newacronym{mof}{MOF}{metal-organic}" in tex


def test_acronym_key_sanitises_digit_lead() -> None:
    assert latex._acronym_key("3D") == "a3d"
    assert latex._acronym_key("RNA-seq") == "rnaseq"


# ── end-to-end against real Postgres ──────────────────────────────────


def test_export_draft_end_to_end(hub, tmp_path) -> None:
    from precis.handlers.draft import DraftHandler

    store = hub.store
    draft = DraftHandler(hub=hub)
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    draft.put(id="nt", title="Nanoscale Transistors", project=proj)
    ref = store.get_ref(kind="draft", id="nt")
    title_h = store.drafts.reading_order(ref.id)[0].handle

    draft.put(
        id="nt", chunk_kind="heading", text="Introduction", at={"after": f"¶{title_h}"}
    )
    sec_h = next(
        c.handle for c in store.drafts.reading_order(ref.id) if c.text == "Introduction"
    )
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="We graft polyethyleneimine (PEI) onto the support; PEI works.",
        at={"into": f"¶{sec_h}", "last": True},
    )
    # define an abbrev as a term chunk → becomes \newacronym + \gls
    draft.put(
        id="nt", chunk_kind="term", text="polyethyleneimine", meta={"short": "PEI"}
    )

    result = latex.export_draft(store, ref, target_dir=tmp_path / "out")
    main = result.main_tex.read_text(encoding="utf-8")

    assert (tmp_path / "out" / "preamble.tex").exists()
    assert r"\documentclass" in main and r"\begin{document}" in main
    assert r"\title{Nanoscale Transistors}" in main
    assert r"\section{Introduction}\label{chunk:" in main
    assert r"\newacronym{pei}{PEI}{polyethyleneimine}" in main
    assert r"\gls{pei}" in main  # surface occurrence glsified
    assert r"\printglossaries" in main and r"\printbibliography" in main
    # the Glossary heading + term chunk are NOT rendered as body sections
    assert r"\section{Glossary}" not in main


def _bib_style_draft(hub):
    from precis.handlers.draft import DraftHandler

    store = hub.store
    draft = DraftHandler(hub=hub)
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    draft.put(id="bs", title="Style Test", project=proj)
    return store, store.get_ref(kind="draft", id="bs")


def test_export_draft_default_bib_style_unchanged(hub, tmp_path) -> None:
    store, ref = _bib_style_draft(hub)
    result = latex.export_draft(store, ref, target_dir=tmp_path / "out")
    main = result.main_tex.read_text(encoding="utf-8")
    pre = result.preamble.read_text(encoding="utf-8")
    assert pre == latex._template_text("preamble.tex")
    assert "style=numeric-comp,sorting=none" in main
    assert not any("bib style" in w for w in result.warnings)


def test_export_draft_explicit_bib_style_lands_in_main_and_preamble(
    hub, tmp_path
) -> None:
    store, ref = _bib_style_draft(hub)
    result = latex.export_draft(
        store, ref, target_dir=tmp_path / "out", bib_style="chem-rsc"
    )
    main = result.main_tex.read_text(encoding="utf-8")
    pre = result.preamble.read_text(encoding="utf-8")
    for text in (main, pre):
        assert "backend=biber,style=chem-rsc,sorting=none" in text
        assert "numeric-comp" not in text
    assert not any("bib style" in w for w in result.warnings)


def test_export_draft_bib_style_alias(hub, tmp_path) -> None:
    store, ref = _bib_style_draft(hub)
    result = latex.export_draft(
        store, ref, target_dir=tmp_path / "out", bib_style="rsc"
    )
    assert "style=chem-rsc," in result.main_tex.read_text(encoding="utf-8")


def test_export_draft_bib_style_falls_back_to_workspace_style(hub, tmp_path) -> None:
    import dataclasses

    store, ref = _bib_style_draft(hub)
    ref = dataclasses.replace(
        ref,
        meta={
            **(ref.meta or {}),
            "workspace": {
                "path": "p",
                "format": "tex",
                "entrypoint": "main.tex",
                "style": "nature",
            },
        },
    )
    result = latex.export_draft(store, ref, target_dir=tmp_path / "ws")
    assert "style=nature," in result.main_tex.read_text(encoding="utf-8")
    # explicit argument beats the workspace
    result = latex.export_draft(
        store, ref, target_dir=tmp_path / "ws2", bib_style="chem-acs"
    )
    assert "style=chem-acs," in result.main_tex.read_text(encoding="utf-8")


def test_export_draft_unknown_bib_style_warns_and_keeps_default(hub, tmp_path) -> None:
    store, ref = _bib_style_draft(hub)
    result = latex.export_draft(
        store, ref, target_dir=tmp_path / "out", bib_style="ieee"
    )
    assert "style=numeric-comp," in result.main_tex.read_text(encoding="utf-8")
    assert any("bib style 'ieee' not supported" in w for w in result.warnings)


def test_export_draft_embeds_raster_figure(hub, tmp_path) -> None:
    """A raster figure emits a \\includegraphics float and materialises the
    image under pics/ beside main.tex."""
    from precis.handlers.draft import DraftHandler

    store = hub.store
    draft = DraftHandler(hub=hub)
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    draft.put(id="nt", title="T", project=proj)
    ref = store.get_ref(kind="draft", id="nt")
    title_h = store.drafts.reading_order(ref.id)[0].handle
    draft.put(
        id="nt",
        chunk_kind="figure",
        text="Fig 1. A widget.",
        image=_PNG_B64,
        origin="original",
        at={"after": f"¶{title_h}"},
    )

    out = tmp_path / "out"
    result = latex.export_draft(store, ref, target_dir=out)
    main = result.main_tex.read_text(encoding="utf-8")
    assert r"\includegraphics" in main and "pics/" in main
    assert r"\caption{Fig 1. A widget.}" in main
    pics = list((out / "pics").glob("*.png"))
    assert len(pics) == 1 and pics[0].read_bytes() == _PNG


def test_render_figure_withheld_emits_box_and_never_loads_asset(hub) -> None:
    """A handle in ``ctx.withheld_figures`` renders a framed box naming the
    publisher and source, keeps the caption, and never embeds the image,
    even though a real blob exists."""
    from precis.handlers.draft import DraftHandler

    store = hub.store
    draft = DraftHandler(hub=hub)
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    draft.put(id="wh", title="T", project=proj)
    ref = store.get_ref(kind="draft", id="wh")
    title_h = store.drafts.reading_order(ref.id)[0].handle
    draft.put(
        id="wh",
        chunk_kind="figure",
        text="Fig 1. Borrowed.",
        image=_PNG_B64,
        origin="third_party",
        permission={
            "publisher": "ACME & Sons",
            "source_paper": "Doe 2020",
            "status": "requested",
        },
        at={"after": f"\u00b6{title_h}"},
    )
    fig = next(
        c for c in store.drafts.reading_order(ref.id) if c.chunk_kind == "figure"
    )
    ctx = latex._Ctx(
        keymap={},
        known_handles=set(),
        store=store,
        withheld_figures=frozenset({fig.dc}),
    )
    out = "\n".join(latex._render_figure(fig, ctx, ""))
    assert "withheld pending permission" in out
    assert r"ACME \& Sons" in out and "Doe 2020" in out
    assert r"\caption{Fig 1. Borrowed.}" in out
    assert r"\includegraphics" not in out
    assert ctx.figures == []
    assert any("withheld" in w and fig.dc in w for w in ctx.warnings)

    # Not in the set: the same figure embeds normally (control).
    ctx2 = latex._Ctx(keymap={}, known_handles=set(), store=store)
    out2 = "\n".join(latex._render_figure(fig, ctx2, ""))
    assert r"\includegraphics" in out2 and len(ctx2.figures) == 1


def test_partition_uncleared_waives_everything_and_withheld_handles() -> None:
    from precis.utils.figure_clearance import (
        FigureClear,
        partition_uncleared,
        withheld_handles,
    )

    img = FigureClear("dc1", "c", "third_party", False, "requested", assetless=False)
    none = FigureClear("dc2", "c", "original", False, "no image yet", assetless=True)
    blocked, waived = partition_uncleared([img, none], placeholder_figures=False)
    assert blocked == [img, none] and waived == []
    blocked, waived = partition_uncleared([img, none], placeholder_figures=True)
    assert blocked == [] and waived == [img, none]
    assert withheld_handles(waived) == frozenset({"dc1"})
    assert withheld_handles([]) == frozenset()


def test_export_draft_include_sources_bundles_appendix(hub, tmp_path, monkeypatch):
    """``include_sources=True`` copies each present cited PDF into
    ``sources/`` and appends a ``pdfpages`` appendix. We stub the cited-
    source resolution so the test needs no held-paper corpus setup."""
    from precis.export import sources as src
    from precis.handlers.draft import DraftHandler

    store = hub.store
    d = DraftHandler(hub=hub)
    proj = store.insert_ref(kind="todo", slug=None, title="P").id
    d.put(id="rep", title="Report", project=proj)
    ref = store.get_ref(kind="draft", id="rep")

    pdf = tmp_path / "smith2020.pdf"
    pdf.write_bytes(b"%PDF-source")
    bundle = src.SourceBundle(
        entries=[
            src.SourceEntry(
                "smith2020", "paper", "A Study", "A. Smith", 2020, "a" * 64, pdf
            )
        ]
    )
    monkeypatch.setattr(src, "collect_cited_sources", lambda *a, **k: bundle)

    out = tmp_path / "out"
    result = latex.export_draft(store, ref, target_dir=out, include_sources=True)

    assert (out / "sources" / "smith2020.pdf").read_bytes() == b"%PDF-source"
    main = result.main_tex.read_text(encoding="utf-8")
    assert r"\includepdf[pages=-]{sources/smith2020.pdf}" in main
    assert result.source_bundle is bundle


def test_export_draft_records_retraction_override_in_appendix(
    hub, tmp_path, monkeypatch
):
    """``retraction_override`` reaches the compiled PDF's sources appendix —
    the trace ``docs/backlog/retraction-override-appendix-trace.md`` shipped
    for (a ``ignore_retractions=1`` override otherwise leaves no mark on the
    artifact itself)."""
    from precis.export import sources as src
    from precis.export.retraction import CitedPaper
    from precis.handlers.draft import DraftHandler

    store = hub.store
    d = DraftHandler(hub=hub)
    proj = store.insert_ref(kind="todo", slug=None, title="P").id
    d.put(id="rep", title="Report", project=proj)
    ref = store.get_ref(kind="draft", id="rep")

    monkeypatch.setattr(
        src, "collect_cited_sources", lambda *a, **k: src.SourceBundle(entries=[])
    )
    out = tmp_path / "out"
    override = [
        CitedPaper(ref_id=1, slug="smith2024", title="Bad Paper", status="retracted")
    ]
    result = latex.export_draft(
        store,
        ref,
        target_dir=out,
        include_sources=True,
        retraction_override=override,
    )
    main = result.main_tex.read_text(encoding="utf-8")
    assert "Retraction override" in main
    assert "smith2024" in main


# ── compile (stub latexmk) ────────────────────────────────────────────


def _stub_latexmk(tmp_path, *, succeed=True):
    """A fake latexmk that touches main.pdf (or not) and exits 0/1 —
    lets us exercise compile_pdf without a TeX install (PRECIS_LATEXMK_BIN
    mirrors the PRECIS_CLAUDE_BIN stub-binary pattern).

    Returns the script path; the caller points ``PRECIS_LATEXMK_BIN`` at it
    via ``monkeypatch.setenv`` (which reverts at teardown). This helper must
    NOT set ``os.environ`` itself — a direct write leaks the stub path into
    other test files under xdist (a failing stub then makes an unrelated
    draft_export compile run it and fail rc=1)."""
    import stat

    script = tmp_path / "latexmk"
    body = "#!/bin/sh\n"
    body += (
        "touch main.pdf\nexit 0\n" if succeed else "echo '! Undefined.' >&2\nexit 1\n"
    )
    script.write_text(body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


@_needs_posix_stub
def test_compile_pdf_success(tmp_path, monkeypatch) -> None:
    from precis.export import compile as cmpl

    monkeypatch.setenv("PRECIS_LATEXMK_BIN", str(_stub_latexmk(tmp_path)))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}x\\end{document}", encoding="utf-8"
    )
    res = cmpl.compile_pdf(proj)
    assert res.ok
    assert res.pdf is not None
    assert res.pdf == proj / "main.pdf" and res.pdf.exists()


@_needs_posix_stub
def test_compile_pdf_failure_returns_log(tmp_path, monkeypatch) -> None:
    from precis.export import compile as cmpl

    monkeypatch.setenv(
        "PRECIS_LATEXMK_BIN", str(_stub_latexmk(tmp_path, succeed=False))
    )
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "main.tex").write_text("broken", encoding="utf-8")
    res = cmpl.compile_pdf(proj)
    assert not res.ok and res.pdf is None and not res.skipped


def test_compile_pdf_skipped_without_latexmk(tmp_path, monkeypatch) -> None:
    from precis.export import compile as cmpl

    monkeypatch.setenv("PRECIS_LATEXMK_BIN", str(tmp_path / "does-not-exist"))
    res = cmpl.compile_pdf(tmp_path)
    assert res.skipped and not res.ok


def test_export_writes_latexmkrc(hub, tmp_path) -> None:
    from precis.handlers.draft import DraftHandler

    store = hub.store
    d = DraftHandler(hub=hub)
    proj = store.insert_ref(kind="todo", slug=None, title="P").id
    d.put(id="nt", title="T", project=proj)
    ref = store.get_ref(kind="draft", id="nt")
    result = latex.export_draft(store, ref, target_dir=tmp_path / "o")
    assert result.latexmkrc.exists()
    assert "makeglossaries" in result.latexmkrc.read_text(encoding="utf-8")


def test_export_renders_itemize_and_enumerate(hub, tmp_path) -> None:
    """ulist→itemize, olist→enumerate, item→\\item (migration 0037)."""
    import re

    from precis.handlers.draft import DraftHandler

    store = hub.store
    d = DraftHandler(hub=hub)
    proj = store.insert_ref(kind="todo", slug=None, title="P").id
    d.put(id="lst", title="T", project=proj)
    ul = d.put(id="lst", chunk_kind="ulist", text="list", at={"last": True})
    ul_h = re.search(r"dc\d+", ul.body).group(0)  # type: ignore[union-attr]
    d.put(id="lst", chunk_kind="item", text="alpha", at={"into": ul_h, "last": True})
    d.put(id="lst", chunk_kind="item", text="beta", at={"into": ul_h, "last": True})
    ol = d.put(id="lst", chunk_kind="olist", text="list", at={"last": True})
    ol_h = re.search(r"dc\d+", ol.body).group(0)  # type: ignore[union-attr]
    d.put(id="lst", chunk_kind="item", text="one", at={"into": ol_h, "last": True})

    ref = store.get_ref(kind="draft", id="lst")
    body = latex.render_body(store, ref).body
    assert "\\begin{itemize}" in body and "\\end{itemize}" in body
    assert "\\begin{enumerate}" in body and "\\end{enumerate}" in body
    assert "\\item alpha" in body and "\\item beta" in body and "\\item one" in body
    # the bullet list closes before the numbered list opens
    assert body.index("\\end{itemize}") < body.index("\\begin{enumerate}")


def test_export_renders_table_as_longtable(hub, tmp_path) -> None:
    """A chunk_kind='table' renders as a booktabs longtable
    — header in \\toprule…\\midrule, every row a `&`-joined `\\\\` line, the
    caption a bold lead-in. Replaces the old "dump the pipe markdown" path."""
    from precis.handlers.draft import DraftHandler

    store = hub.store
    d = DraftHandler(hub=hub)
    proj = store.insert_ref(kind="todo", slug=None, title="P").id
    d.put(id="tb", title="T", project=proj)
    d.put(
        id="tb",
        chunk_kind="table",
        table={"header": ["ID", "Title"], "rows": [["I1", "loss & gain"], ["I2", "x"]]},
        caption="Issue register",
        at={"last": True},
    )
    ref = store.get_ref(kind="draft", id="tb")
    body = latex.render_body(store, ref).body
    assert "\\begin{longtable}" in body and "\\end{longtable}" in body
    assert "\\toprule" in body and "\\midrule" in body and "\\bottomrule" in body
    assert "ID & Title \\\\" in body
    # cells go through the inline escaper (& → \&); caption is a bold lead-in
    assert "I1 & loss \\& gain \\\\" in body
    assert "\\textbf{Issue register}" in body
    # the derived pipe markdown is NOT dumped as prose
    assert "| ID | Title |" not in body


def test_standalone_equation_numbers_and_cross_refs(hub, tmp_path) -> None:
    """A paragraph chunk whose ENTIRE text is one ``$$…$$`` span exports as
    a numbered ``equation`` environment, labeled INSIDE it — and an
    existing ``[dc<id>]`` cross-ref to that chunk auto-resolves through the
    unchanged ``\\cref``/``_draft_xref`` machinery, no new authoring syntax."""
    from precis.handlers.draft import DraftHandler

    store = hub.store
    d = DraftHandler(hub=hub)
    proj = store.insert_ref(kind="todo", slug=None, title="P").id
    d.put(id="eq", title="T", project=proj)
    eq1 = d.put(id="eq", chunk_kind="paragraph", text="$$E = mc^2$$", at={"last": True})
    dc1 = re.search(r"dc\d+", eq1.body).group(0)  # type: ignore[union-attr]
    d.put(
        id="eq",
        chunk_kind="paragraph",
        text=f"As shown in [{dc1}], mass and energy relate.",
        at={"last": True},
    )
    d.put(id="eq", chunk_kind="paragraph", text="$$F = ma$$", at={"last": True})

    ref = store.get_ref(kind="draft", id="eq")
    body = latex.render_body(store, ref).body
    assert f"\\begin{{equation}}\n\\label{{chunk:{dc1}}}" in body
    assert body.count("\\begin{equation}") == 2 and body.count("\\end{equation}") == 2
    assert f"\\cref{{chunk:{dc1}}}" in body  # auto-resolves via existing xref machinery


def test_starred_equation_is_unnumbered(hub, tmp_path) -> None:
    """A trailing ``*`` right after the closing ``$$`` opts a display
    equation out of numbering (the LaTeX ``equation``/``equation*``
    convention) — renders through the ordinary math path, no ``equation``
    environment at all."""
    from precis.handlers.draft import DraftHandler

    store = hub.store
    d = DraftHandler(hub=hub)
    proj = store.insert_ref(kind="todo", slug=None, title="P").id
    d.put(id="eqs", title="T", project=proj)
    d.put(
        id="eqs",
        chunk_kind="paragraph",
        text="$$a^2 + b^2 = c^2$$*",
        at={"last": True},
    )

    ref = store.get_ref(kind="draft", id="eqs")
    body = latex.render_body(store, ref).body
    assert "\\begin{equation}" not in body
    assert "$$a^2 + b^2 = c^2$$" in body
    assert "$$*" not in body  # the star marker itself never leaks into output


# ── author byline + affiliations (authblk; no DB) ─────────────────────


class TestBuildAuthorBlock:
    def test_no_authors_falls_back_to_string(self) -> None:
        assert latex.build_author_block(None, fallback="precis") == "\\author{precis}"

    def test_distinct_affiliations_numbered_with_ror_href(self) -> None:
        raw = [
            {
                "name": "Doe, Jane",
                "affiliation": "MIT & Co",
                "ror": "https://ror.org/x",
            },
            {"name": "Roe, John", "affiliation": "Caltech"},
        ]
        out = latex.build_author_block(raw, fallback="precis")
        assert "\\author[1]{Jane Doe}" in out
        assert "\\author[2]{John Roe}" in out
        # org name is escaped (& → \&) and hyperlinked to its ROR id
        assert "\\affil[1]{\\href{https://ror.org/x}{MIT \\& Co}}" in out
        assert "\\affil[2]{Caltech}" in out

    def test_single_shared_affiliation_is_unnumbered(self) -> None:
        raw = [
            {"name": "A B", "affiliation": "MIT", "ror": "r1"},
            {"name": "C D", "affiliation": "MIT", "ror": "r1"},
        ]
        out = latex.build_author_block(raw, fallback="precis")
        assert "\\author{A B}" in out and "\\author{C D}" in out
        assert "[1]" not in out  # no superscript numbers for a single affiliation
        assert out.count("\\affil{") == 1

    def test_orcid_renders_as_orcidlink_after_the_name(self) -> None:
        raw = [
            {"name": "A B", "affiliation": "MIT", "orcid": "0000-0002-1825-0097"},
            {"name": "C D", "affiliation": "Caltech"},
        ]
        out = latex.build_author_block(raw, fallback="precis")
        assert "\\author[1]{A B\\,\\orcidlink{0000-0002-1825-0097}}" in out
        assert "\\author[2]{C D}" in out
        # the preamble defines the macro, so the byline compiles everywhere
        preamble = (
            Path(latex.__file__).parents[1]
            / "data"
            / "templates"
            / "draft"
            / "preamble.tex"
        ).read_text(encoding="utf-8")
        assert "\\providecommand{\\orcidlink}[1]" in preamble


def test_export_draft_emits_byline_from_ref_authors(hub) -> None:
    """End-to-end: authors set on the draft ref flow into main.tex."""
    from precis.handlers.draft import DraftHandler

    store = hub.store
    d = DraftHandler(hub=hub)
    proj = store.insert_ref(kind="todo", slug=None, title="P").id
    d.put(id="byl", title="A Study", project=proj)
    d.edit(
        id="byl",
        authors=[
            {"name": "Doe, Jane", "affiliation": "MIT", "ror": "https://ror.org/x"},
            {"name": "Roe, John", "affiliation": "Caltech"},
        ],
    )
    ref = store.get_ref(kind="draft", id="byl")
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        latex.export_draft(store, ref, target_dir=Path(td))
        main_tex = (Path(td) / "main.tex").read_text(encoding="utf-8")
    assert "\\author[1]{Jane Doe}" in main_tex
    assert "\\affil[1]{\\href{https://ror.org/x}{MIT}}" in main_tex
    assert "\\affil[2]{Caltech}" in main_tex


def test_markdown_bullets_export_as_a_nested_itemize(hub, tmp_path) -> None:
    """End to end, the trap this closes: bullet text written into a
    paragraph used to reach LaTeX as one run-on line with literal hyphens.
    It now lands structured (:mod:`precis.draft.mdlist`) and renders as a
    real nested itemize."""
    from precis.handlers.draft import DraftHandler

    store = hub.store
    d = DraftHandler(hub=hub)
    proj = store.insert_ref(kind="todo", slug=None, title="P").id
    d.put(id="md", title="T", project=proj)
    d.put(
        id="md",
        chunk_kind="paragraph",
        text="- NO side\n    - Bader charge shows 0.39 e\n- NH3 side",
        at={"last": True},
    )

    ref = store.get_ref(kind="draft", id="md")
    body = latex.render_body(store, ref).body
    assert body.count("\\begin{itemize}") == 2  # outer + the nested one
    assert body.count("\\end{itemize}") == 2
    assert "\\item NO side" in body and "\\item NH3 side" in body
    # the sublist sits between its parent item and the next sibling
    assert (
        body.index("\\item NO side")
        < body.index("\\item Bader charge shows 0.39 e")
        < body.index("\\item NH3 side")
    )


# ── export defects from the dr173020 visual read ──────────────────────


def _one_paper_bib(title, journal=None):
    meta = {"journal": journal} if journal else None
    store = _BibStore(
        {("paper", "p1"): _bibref(1, "p1", "paper", title=title, year=2020, meta=meta)}
    )
    return latex.build_bib(store, ["p1"], [])


def test_build_bib_title_html_tags_become_text_commands() -> None:
    bib = _one_paper_bib(
        "Carbon nanotube–fullerene hybrid by C<sub>60</sub> bombardment of "
        "<i>In situ</i> <scp>iii</scp> <b>x</b><sup>2</sup> <span>y</span>"
    )
    assert r"C\textsubscript{60}" in bib
    assert r"\emph{In situ}" in bib
    assert r"\textsc{iii}" in bib
    assert r"\textbf{x}\textsuperscript{2}" in bib
    assert "<" not in bib and "span" not in bib


def test_build_bib_title_math_passes_through_and_protective_braces_drop() -> None:
    bib = _one_paper_bib("Encapsulated {C$_{60}$} in carbon nanotubes")
    assert "title = {Encapsulated C$_{60}$ in carbon nanotubes}" in bib


def test_build_bib_journal_decodes_entities_and_tags() -> None:
    bib = _one_paper_bib("T", journal="Materials &amp; Design")
    assert "journaltitle = {Materials \\& Design}" in bib
    bib = _one_paper_bib("T", journal="J. <i>Chem</i> Phys")
    assert r"journaltitle = {J. \emph{Chem} Phys}" in bib


def test_adjacent_identical_cites_collapse_to_one(monkeypatch) -> None:
    import types

    monkeypatch.setattr(latex, "_trust_mark_latex", lambda _c, _r: "")

    store = _FindingStore(
        {
            1: types.SimpleNamespace(meta={"primary_cite_key": "choi16"}),
            2: types.SimpleNamespace(meta={"primary_cite_key": "choi16"}),
            3: types.SimpleNamespace(meta={"primary_cite_key": "other20"}),
        }
    )
    ctx = latex._Ctx(keymap={}, known_handles=set(), store=store)
    out = latex._render_inline("a [fi1][fi2][fi1] b [fi1] [fi2] c [fi1][fi3].", ctx)
    assert out.count(r"\cite{choi16}") == 3  # run, then each spaced cite
    assert r"\cite{choi16,other20}" in out  # distinct keys still merge


def test_taproot_hub_without_own_source_uses_conjunct_cites(monkeypatch) -> None:
    import types

    store = _FindingStore({7: types.SimpleNamespace(meta={})})
    monkeypatch.setattr(latex, "_conjunct_cite_keys", lambda _s, _pk: ["a21", "b22"])
    ctx = latex._Ctx(keymap={}, known_handles=set(), store=store)
    out = latex._render_inline("claim [fi7].", ctx)
    assert r"\cite{a21,b22}" in out
    assert ctx.warnings == []


def test_conjunct_cite_keys_unions_atoms_deduped(monkeypatch) -> None:
    import types

    from precis.taproot import cite as tcite
    from precis.taproot import seniority

    monkeypatch.setattr(
        seniority, "conjunct_atoms_bulk", lambda _s, ids: {ids[0]: [11, 12, 13]}
    )
    keys = {11: ["a21"], 12: ["a21", "b22"], 13: []}
    monkeypatch.setattr(
        tcite,
        "finding_cite_keys",
        lambda _s, rid: types.SimpleNamespace(cite_keys=keys[rid]),
    )
    assert latex._conjunct_cite_keys(object(), 7) == ["a21", "b22"]


def test_sourceless_hub_warns_and_pulls_space_before_punctuation(monkeypatch) -> None:
    import types

    store = _FindingStore({7: types.SimpleNamespace(meta={})})
    monkeypatch.setattr(latex, "_trust_mark_latex", lambda _c, _r: "")
    monkeypatch.setattr(latex, "_conjunct_cite_keys", lambda _s, _pk: [])
    ctx = latex._Ctx(keymap={}, known_handles=set(), store=store)
    out = latex._render_inline("in frameworks [fi7].", ctx)
    assert out.startswith("in frameworks.")
    assert "cite" not in out
    assert ctx.warnings == [
        "cite [fi7]: no citable source (conjunction hub without sourced conjuncts)"
    ]


def test_glsify_collapses_spelled_out_first_use() -> None:
    abbrevs = {"PGNB": "periodic graphene nanobud"}
    out, _ = _inline("periodic graphene nanobuds (PGNBs) were built", abbrevs)
    # chunk start is a sentence start → capitalised first-use form
    assert out.count(r"\Glspl{pgnb}") == 1
    assert "(PGNBs)" not in out
    assert out.startswith(r"\Glspl{pgnb} were built")
    mid, _ = _inline("we built periodic graphene nanobuds (PGNBs) here", abbrevs)
    assert mid == r"we built \glspl{pgnb} here"
    ctx = _ctx("x", abbrevs)
    two = latex._render_inline("Periodic Graphene Nanobud (PGNB) and then PGNBs.", ctx)
    assert two.count("{pgnb}") == 2
    assert r"\glspltip{pgnb}" in two


def test_glsify_leaves_unspelled_first_use_alone() -> None:
    out, _ = _inline("we grew PGNB films", {"PGNB": "periodic graphene nanobud"})
    assert r"\gls{pgnb}" in out


def test_glsify_sentence_initial_first_use_capitalised() -> None:
    abbrevs = {"GGA": "generalized gradient approximation"}
    out, _ = _inline("GGA functionals fail.", abbrevs)
    assert out.startswith(r"\Gls{gga}")
    out, _ = _inline("It fails. GGA functionals too.", abbrevs)
    assert r"\Gls{gga}" in out
    out, _ = _inline("It fails with GGA functionals.", abbrevs)
    assert r"\gls{gga}" in out


def _bib_one(meta, *, authors=None, title="T"):
    ref = _bibref(1, "p1", "paper", title=title, authors=authors, meta=meta)
    return latex.build_bib(_BibStore({("paper", "p1"): ref}), ["p1"], [])


def test_build_bib_book_emits_publisher_isbn_editor() -> None:
    eds = [{"given": "Ann", "family": "Lee"}]
    bib = _bib_one(
        {
            "entry_type": "book",
            "publisher": "Springer",
            "isbn": "978-3-16-148410-0",
            "editors": eds,
            "journal": "Ignored",
        },
        authors=eds,
    )
    assert bib.startswith("@book{p1,")
    assert "publisher = {Springer}" in bib
    assert "isbn = {978-3-16-148410-0}" in bib
    assert "editor = {Ann Lee}" in bib
    assert "author =" not in bib
    assert "journaltitle" not in bib


def test_build_bib_chapter_emits_booktitle_and_pages() -> None:
    bib = _bib_one(
        {
            "entry_type": "book-chapter",
            "container_title": "Handbook of Things",
            "pages": "10-20",
            "publisher": "Wiley",
        },
        authors=[{"given": "Bo", "family": "Kim"}],
    )
    assert bib.startswith("@incollection{p1,")
    assert "booktitle = {Handbook of Things}" in bib
    assert "pages = {10-20}" in bib
    assert "author = {Bo Kim}" in bib


def test_build_bib_other_crossref_types() -> None:
    assert _bib_one({"entry_type": "proceedings-article"}).startswith("@inproceedings{")
    assert _bib_one({"entry_type": "dissertation", "publisher": "MIT"}).startswith(
        "@thesis{"
    )
    assert "institution = {MIT}" in _bib_one(
        {"entry_type": "report", "publisher": "MIT", "url": "https://x.org/a"}
    )


def test_build_bib_article_for_journal_or_missing_entry_type() -> None:
    assert _bib_one({"entry_type": "journal-article"}).startswith("@article{")
    assert _bib_one({"journal": "J"}).startswith("@article{")
    assert _bib_one(None).startswith("@article{")


# ── supplementary-information records cite as their parent ───────────────


class _SIStore:
    """Fake store with a parent paper and an SI record (``pdf_role``
    supplement). ``supplement_parent`` is monkeypatched, so ``pool`` is a
    null-connection stub."""

    def __init__(self, refs):
        import contextlib

        self._refs = refs
        self.pool = type(
            "P", (), {"connection": lambda s: contextlib.nullcontext(object())}
        )()

    def get_ref(self, *, kind, id):
        return self._refs.get((kind, id))

    def identifiers_for_refs(self, ref_ids):
        return {}


def _si_store(monkeypatch, *, with_parent: bool):
    from types import SimpleNamespace

    from precis.store import si_links

    parent = _bibref(1, "parent24", "paper", title="Parent paper", year=2024)
    si = SimpleNamespace(
        **{
            **vars(_bibref(2, "parent24si", "paper", title="SI", year=2024)),
            "pdf_role": "supplement",
        }
    )
    monkeypatch.setattr(
        si_links,
        "supplement_parent",
        lambda _conn, rid: (1, "parent24") if with_parent and rid == 2 else None,
    )
    return _SIStore({("paper", "parent24"): parent, ("paper", "parent24si"): si})


def test_si_cite_resolves_to_parent_with_postnote(monkeypatch) -> None:
    store = _si_store(monkeypatch, with_parent=True)
    out, ctx = _inline("see [§parent24si~3] here", store=store)
    assert r"\cite[SI]{parent24}" in out
    assert "parent24si" not in out
    assert ctx.cited == ["parent24"] and ctx.si_cited == ["parent24"]
    bib = latex.build_bib(store, ctx.cited, [])
    assert "Parent paper" in bib and "parent24si" not in bib


def test_si_cite_group_uses_cites_with_postnote_on_si_key_only(monkeypatch) -> None:
    store = _si_store(monkeypatch, with_parent=True)
    ctx = _ctx("", store=store)
    out = latex._cite_keys(["other", "parent24si"], ctx)
    assert out.startswith(r"\cites{other}[SI]{parent24}")
    assert ctx.cited == ["other", "parent24"] and ctx.si_cited == ["parent24"]


def test_multicite_is_terminated_before_link_group(monkeypatch) -> None:
    store = _si_store(monkeypatch, with_parent=True)
    ctx = _ctx("", store=store)
    monkeypatch.setattr(latex, "_cite_link_group", lambda _k, _c: r"{\scriptsize L}")
    out = latex._merge_adjacent_cites(
        latex._cite_keys(["other", "parent24si"], ctx), ctx
    )
    assert out == r"\cites{other}[SI]{parent24}\relax{\scriptsize L}"


def test_aliases_canonicalise_to_one_key_and_one_bib_entry(monkeypatch) -> None:
    from types import SimpleNamespace

    store = _si_store(monkeypatch, with_parent=True)
    canon = _bibref(1, "canon", "paper", title="Canon paper", year=2024)
    si = SimpleNamespace(
        **{**vars(_bibref(2, "sirec", "paper", title="SI")), "pdf_role": "supplement"}
    )
    store._refs = {
        ("paper", "canon"): canon,
        ("paper", "alias1"): canon,
        ("paper", "sirec"): si,
    }
    # SI parent is reached through the alias key
    monkeypatch.setattr(
        "precis.store.si_links.supplement_parent", lambda _c, rid: (1, "alias1")
    )
    ctx = _ctx("", store=store)
    out = latex._cite_keys(["alias1", "canon"], ctx)
    assert out.startswith(r"\cite{canon}") and "alias1" not in out
    out_si = latex._cite_keys(["sirec"], _ctx("", store=store))
    assert out_si.startswith(r"\cite[SI]{canon}")
    assert ctx.cited == ["canon"]
    bib = latex.build_bib(store, ctx.cited, [])
    assert bib.count("@") == 1 and "alias1" not in bib


def test_plain_cite_group_stays_cite(monkeypatch) -> None:
    store = _si_store(monkeypatch, with_parent=True)
    ctx = _ctx("", store=store)
    out = latex._cite_keys(["parent24", "other"], ctx)
    assert out.startswith(r"\cite{parent24,other}")
    assert ctx.si_cited == []


def test_si_cite_without_parent_falls_back_with_warning(monkeypatch) -> None:
    store = _si_store(monkeypatch, with_parent=False)
    out, ctx = _inline("see [§parent24si] here", store=store)
    assert r"\cite{parent24si}" in out and "[SI]" not in out
    assert ctx.cited == ["parent24si"] and ctx.si_cited == []
    assert any("no live parent" in w for w in ctx.warnings)


def _link_ctx(monkeypatch):
    ctx = _ctx("")
    monkeypatch.setattr(
        latex, "_cite_link_group", lambda keys, _c: "{L:" + "+".join(keys) + "}"
    )
    return ctx


def test_adjacent_cites_merge_with_one_link_group(monkeypatch) -> None:
    ctx = _link_ctx(monkeypatch)
    s = latex._cite("a", ctx) + latex._cite("b", ctx)
    assert latex._merge_adjacent_cites(s, ctx) == r"\cite{a,b}{L:a+b}"


def test_overlapping_cites_dedupe_keys(monkeypatch) -> None:
    ctx = _link_ctx(monkeypatch)
    s = latex._cite("a", ctx) + latex._cite_keys(["a", "b"], ctx)
    assert latex._merge_adjacent_cites(s, ctx) == r"\cite{a,b}{L:a+b}"


def test_si_cite_not_merged(monkeypatch) -> None:
    ctx = _link_ctx(monkeypatch)
    s = latex._cite("a", ctx) + latex._cite("b", ctx, si=True) + latex._cite("c", ctx)
    out = latex._merge_adjacent_cites(s, ctx)
    assert out == r"\cite{a}{L:a}\cite[SI]{b}{L:b}\cite{c}{L:c}"


def test_text_between_cites_prevents_merge(monkeypatch) -> None:
    ctx = _link_ctx(monkeypatch)
    s = latex._cite("a", ctx) + " " + latex._cite("b", ctx)
    assert latex._merge_adjacent_cites(s, ctx) == r"\cite{a}{L:a} \cite{b}{L:b}"


def test_adjacent_cites_without_links_unchanged() -> None:
    ctx = _ctx("")
    ctx.doi_links = ctx.library_links = False
    s = latex._cite("a", ctx) + latex._cite("b", ctx)
    assert latex._merge_adjacent_cites(s, ctx) == r"\cite{a,b}"
    assert latex._merge_adjacent_cites(latex._cite("a", ctx), ctx) == r"\cite{a}"


def test_glsify_collapses_long_form_containing_another_short() -> None:
    abbrevs = {"CNB": "carbon nanobud", "FCNB": "functionalized CNB"}
    out, _ = _inline("a functionalized CNB (FCNB) and FCNBs", abbrevs)
    assert out.count(r"\gls{fcnb}") == 1
    assert "functionalized" not in out and "(FCNB)" not in out
    assert r"\glspltip{fcnb}" in out
    assert "{cnb}" not in out


# ── cross-ref polish (_polish_xrefs / nameref) ────────────────────────


def test_adjacent_crefs_merge() -> None:
    out, _ = _inline("See [dc1][dc2] and [dc3] [dc4].")
    assert r"\cref{chunk:dc1,chunk:dc2}" in out
    assert r"\cref{chunk:dc3,chunk:dc4}" in out


def test_cref_after_cite_gets_space() -> None:
    out, _ = _inline("Shown by paper:smith2024[dc7].")
    assert r"\cite{smith2024} \cref{chunk:dc7}" in out
    # also past a trailing link run
    s = latex._polish_xrefs(r"x \cite{a}{\scriptsize \href{u}{doi}}\cref{chunk:dc7}")
    assert s.endswith(r"} \cref{chunk:dc7}")


def test_cref_capitalised_at_start_and_after_sentence() -> None:
    out, _ = _inline("[dc1] shows it. [dc2] too, see [dc3]; what? [dc4]")
    assert r"\Cref{chunk:dc1}" in out
    assert r"\Cref{chunk:dc2}" in out
    assert r"\cref{chunk:dc3}" in out
    assert r"\Cref{chunk:dc4}" in out


def test_cref_to_paragraph_heading_uses_nameref() -> None:
    ctx = latex._Ctx(
        keymap={},
        known_handles={"dc1", "dc2"},
        paragraph_handles=frozenset({"dc2"}),
    )
    out = latex._render_inline("Look at the part in [dc2] and [dc1].", ctx)
    assert r"\nameref{chunk:dc2}" in out
    assert r"\cref{chunk:dc1}" in out


def test_render_body_deep_heading_xref_is_nameref(hub) -> None:
    from precis.handlers.draft import DraftHandler

    store = hub.store
    d = DraftHandler(hub=hub)
    proj = store.insert_ref(kind="todo", slug=None, title="P").id
    d.put(id="nr", title="T", project=proj)
    ref = store.get_ref(kind="draft", id="nr")
    parent = store.drafts.reading_order(ref.id)[0]
    deep = ""
    for n in range(4):
        d.put(
            id="nr",
            chunk_kind="heading",
            text=f"Level {n}",
            at={"into": "¶" + parent.handle, "last": True},
        )
        parent = store.drafts.reading_order(ref.id)[-1]
        deep = parent.dc
    d.put(
        id="nr", chunk_kind="paragraph", text=f"Refer to [{deep}].", at={"last": True}
    )
    chunks = store.drafts.reading_order(ref.id)
    body = latex.render_body(store, ref).body
    depths = {c.dc: c.depth for c in chunks}
    assert depths[deep] >= 3, depths
    assert f"\\label{{chunk:{deep}}}" in body
    assert f"\\nameref{{chunk:{deep}}}" in body


def test_back_matter_headings_are_unnumbered(hub, tmp_path) -> None:
    """Journal back matter (Acknowledgements, Notes, ...) and its
    subsections export as starred headings with a TOC line; ordinary
    sections and a non-matching nested heading stay numbered; a cross-ref
    to a back-matter heading uses \\nameref."""
    from precis.handlers.draft import DraftHandler

    store = hub.store
    d = DraftHandler(hub=hub)
    proj = store.insert_ref(kind="todo", slug=None, title="P").id
    d.put(id="bm", title="Paper", project=proj)
    ref = store.get_ref(kind="draft", id="bm")
    title = store.drafts.reading_order(ref.id)[0]

    def heading(text, after=None, into=None):
        at = {"after": "¶" + after} if after else {"into": "¶" + into, "last": True}
        d.put(id="bm", chunk_kind="heading", text=text, at=at)
        return next(
            c for c in store.drafts.reading_order(ref.id) if c.text == text.strip()
        )

    intro = heading("Introduction", after=title.handle)
    ack = heading("  acknowledgements ", after=intro.handle)
    sub = heading("Funding", into=ack.handle)
    other = heading("Outlook", after=ack.handle)
    sub2 = heading("Details", into=other.handle)
    d.put(
        id="bm",
        chunk_kind="paragraph",
        text=f"Thanks, see [{sub.dc}].",
        at={"last": True},
    )

    body = latex.render_body(store, ref).body

    assert "\\section{Introduction}" in body
    assert "\\section*{acknowledgements}" in body
    assert "\\addcontentsline{toc}{section}{acknowledgements}" in body
    assert "\\subsection*{Funding}" in body
    assert "\\addcontentsline{toc}{subsection}{Funding}" in body
    assert "\\section{Outlook}" in body
    assert "\\subsection{Details}" in body
    assert f"\\nameref{{chunk:{sub.dc}}}" in body
    assert sub2.dc not in latex._back_matter_handles(store.drafts.reading_order(ref.id))


def test_render_body_skips_seeded_title_heading_and_lifts_depths(hub) -> None:
    """``create_draft`` seeds a depth-0 heading with the title; ``\\maketitle``
    prints it already, so it must not also open the body as section 1 (which
    numbered every real section 1.x). Its label survives for cross-refs."""
    from precis.handlers.draft import DraftHandler

    store = hub.store
    d = DraftHandler(hub=hub)
    proj = store.insert_ref(kind="todo", slug=None, title="P").id
    d.put(id="tt", title="Carbon Nanobuds", project=proj)
    ref = store.get_ref(kind="draft", id="tt")
    title = store.drafts.reading_order(ref.id)[0]
    d.put(
        id="tt",
        chunk_kind="heading",
        text="Introduction",
        at={"into": "¶" + title.handle, "last": True},
    )
    intro = store.drafts.reading_order(ref.id)[-1]
    d.put(
        id="tt",
        chunk_kind="heading",
        text="Scope",
        at={"into": "¶" + intro.handle, "last": True},
    )
    body = latex.render_body(store, ref).body
    assert "\\section{Carbon Nanobuds}" not in body
    assert f"\\phantomsection\\label{{chunk:{title.dc}}}" in body
    assert "\\section{Introduction}" in body
    assert "\\subsection{Scope}" in body

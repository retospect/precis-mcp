"""gr515227 acceptance tests for the shared title views (raw/plain/md)."""

from __future__ import annotations

import pytest

from precis.utils.title_views import title_md, title_md_clip, title_plain

CASE = "Pd<sup>II</sup> on CO<sub>2</sub>"


def test_plain_sub_sup() -> None:
    assert title_plain(CASE) == "PdII on CO2"


def test_md_sub_sup() -> None:
    assert title_md(CASE) == "Pd$^{\\mathrm{II}}$ on CO$_{2}$"


def test_plain_arxiv_math() -> None:
    assert title_plain(r"CO$_2$") == "CO2"
    assert title_plain(r"$\mathrm{CO_2}$ adsorption") == "CO2 adsorption"
    assert title_plain(r"C$_{60}$ fullerene") == "C60 fullerene"


def test_md_arxiv_math_passes_through() -> None:
    raw = r"$\mathrm{CO_2}$ adsorption"
    assert title_md(raw) == raw


def test_dollar_not_math() -> None:
    assert title_plain("The $100 Laptop") == "The 100 Laptop"
    assert title_md("The $100 Laptop") == "The \\$100 Laptop"


def test_italic_and_bold() -> None:
    assert title_md("<i>in situ</i> study") == "*in situ* study"
    assert title_md("<b>key</b> result") == "**key** result"
    assert title_plain("<i>in situ</i> study") == "in situ study"


def test_sc_plain() -> None:
    assert title_md("<scp>ISBT</scp> 2022") == "ISBT 2022"
    assert title_plain("<sc>SigKdd</sc> conference") == "SigKdd conference"


def test_nested_tags() -> None:
    assert title_md("<b><i>x</i></b>") == "***x***"
    assert title_plain("<i><sub>y</sub></i>H2O") == "yH2O"
    assert title_md("H<sub>2</sub>O<i><sub>x</sub></i>") == "H$_{2}$O*$_{\\mathrm{x}}$*"


def test_unknown_tags_dropped_content_kept() -> None:
    assert title_plain("a<foo>real</foo>b") == "a real b"
    assert title_md("a<foo>real</foo>b") == "a real b"


def test_entities() -> None:
    assert title_plain("A&amp;B &#8594; C") == "A&B → C"
    assert title_md("AT&amp;T") == "AT&T"
    assert title_plain("C&lt;sub&gt;x&lt;/sub&gt;") == "Cx"


def test_mathml() -> None:
    raw = "x<mml:math><mml:msub><mml:mi>CO</mml:mi><mml:mn>2</mml:mn></mml:msub></mml:math>"
    assert title_plain(raw) == "x CO2"
    assert title_md(raw) == "x$CO_{2}$"


def test_malformed_and_unclosed() -> None:
    assert title_plain("a<sup>b") == "ab"
    assert title_md("a<sup>b") == "a$^{\\mathrm{b}}$"
    assert title_plain("a</sup>b") == "ab"
    assert title_md("a</sup>b") == "ab"
    assert title_plain("Pd<sup>II") == "PdII"
    assert title_md("Pd<sup>II") == "Pd$^{\\mathrm{II}}$"
    assert title_md("<sup") == ""  # an incomplete tag at EOF is markup noise
    assert title_plain("<") == "<"  # a lone `<` is text, passes through


def test_injection_is_text() -> None:
    raw = 'x<script>alert(1)</script><img src onerror="pwn()">y'
    assert title_plain(raw) == "x alert(1)y"
    md = title_md(raw)
    assert "<" not in md and "script" not in md and "onerror" not in md


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_empty(raw: str | None) -> None:
    assert title_plain(raw) == ""
    assert title_md(raw) == ""


def test_short_italic_glues_in_plain() -> None:
    # ≤2-char inline pair is a sub/superscript in disguise: stays tight.
    assert title_plain("(Me<sub>3</sub>Si)<sub>2</sub>N") == "(Me3Si)2N"
    # A longer inline span marks a word boundary.
    assert title_plain("using<i>IFEFFIT</i>code") == "using IFEFFIT code"


def test_superscript_number_signs() -> None:
    assert title_md("g<sup>-1</sup>") == "g$^{-1}$"
    assert title_md("rate<sup>3+</sup>") == "rate$^{3+}$"


def test_no_markup_passthrough() -> None:
    raw = "Plain title, nothing special (2024)"
    assert title_plain(raw) == raw
    assert title_md(raw) == raw


def test_md_clip_never_splits_math_or_emphasis() -> None:
    # A cut inside $…$ would leave a dangling $ that reads as noise —
    # worse, it could pair with a later literal $ as fake math.
    md = "A long title about " + "x" * 40 + " $C_{60}$ and more words here"
    clipped = title_md_clip(md, 50)
    assert clipped.endswith("…") and "$C_{60}$" not in clipped
    assert clipped.count("$") == 0
    # A span that fits before the cap survives whole.
    md2 = "Short $C_{60}$ then " + "y" * 60
    out2 = title_md_clip(md2, 30)
    assert "$C_{60}$" in out2 and out2.endswith("…")
    # Emphasis is atomic too: a cap landing inside **…** drops the whole
    # span back to its start instead of halving it.
    md3 = "pre **bold words** post " + "z" * 60
    clipped3 = title_md_clip(md3, 10)
    assert clipped3 == "pre…"


def test_md_clip_pulls_back_dangling_backslash() -> None:
    # The \$ literal-dollar escape must not be halved into a trailing `\`.
    md = "aaaaa\\$" + "b" * 60
    assert title_md_clip(md, 6) == "aaaaa…"
    assert not title_md_clip(md, 6).endswith("\\…")

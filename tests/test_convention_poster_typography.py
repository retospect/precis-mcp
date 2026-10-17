"""Enforcers for docs/conventions/poster-typography.md on the packaged template.

Checks (text-level, no LaTeX needed; the render smoke is test_poster_template):

- ``citations-do-not-wrap``: ``\\src`` is defined with ``\\unskip\\nobreak\\ ``.
- ``band-sizing``: band titles use ``\\vphantom{Ay}`` and never ``\\strut``.
- ``math-and-units``: ``\\boldmath`` inside a ``\\newcommand`` body must sit in its
  own nested group (it is a switch, not an argument).
- ``layout-and-face``: ``\\hyphenpenalty=10000`` is set for block titles.

Judgement-only bullets (upright units, serif vs sans, slash ranges, vskip
ratios) have no crisp static test.
"""

from __future__ import annotations

import re
from importlib import resources

_TEMPLATE_FILES = ("poster.tex", "preamble.tex")


def _template_text() -> str:
    root = resources.files("precis.data.templates.draft")
    return "\n".join(
        (root / n).read_text(encoding="utf-8")
        for n in _TEMPLATE_FILES
        if (root / n).is_file()
    )


def _strip_comments(tex: str) -> str:
    return re.sub(r"(?<!\\)%.*", "", tex)


def src_violations(tex: str) -> list[str]:
    tex = _strip_comments(tex)
    m = re.search(r"\\newcommand\{\\src\}\[1\]\{(.*)\}", tex)
    if m is None:
        return ["\\src not defined"]
    body = m.group(1)
    if not body.startswith("\\unskip\\nobreak\\ "):
        return [f"\\src body must start with \\unskip\\nobreak\\ : {body[:40]!r}"]
    return []


def band_violations(tex: str) -> list[str]:
    tex = _strip_comments(tex)
    out = []
    if "\\strut" in tex:
        out.append("\\strut in band macros (grows bands); use \\vphantom{Ay}")
    if "\\vphantom{Ay}" not in tex:
        out.append("band title lacks \\vphantom{Ay}")
    return out


def hyphen_violations(tex: str) -> list[str]:
    if "\\hyphenpenalty=10000" not in _strip_comments(tex):
        return ["\\hyphenpenalty=10000 missing"]
    return []


def boldmath_violations(tex: str) -> list[str]:
    """``\\boldmath`` at depth 0 of a ``\\newcommand`` body leaks past the argument."""
    tex = _strip_comments(tex)
    out = []
    for m in re.finditer(r"\\newcommand\{(\\\w+)\}(?:\[\d\])?\{", tex):
        depth = 1
        i = m.end()
        while i < len(tex) and depth:
            c = tex[i]
            if c == "\\":
                if tex.startswith("\\boldmath", i) and depth == 1:
                    out.append(f"{m.group(1)}: \\boldmath not in its own group")
                i += 2
                continue
            depth += c == "{"
            depth -= c == "}"
            i += 1
    return out


def test_template_citations_do_not_wrap() -> None:
    assert not src_violations(_template_text())


def test_template_band_sizing() -> None:
    assert not band_violations(_template_text())


def test_template_boldmath_scoped() -> None:
    assert not boldmath_violations(_template_text())


def test_template_hyphenpenalty() -> None:
    assert not hyphen_violations(_template_text())


def test_checkers_fire_on_synthetic_violations() -> None:
    assert src_violations(r"\newcommand{\src}[1]{{\footnotesize [#1]}}")
    assert not src_violations(r"\newcommand{\src}[1]{\unskip\nobreak\ {[#1]}}")
    assert band_violations(r"\strut\vphantom{Ay}")
    assert band_violations(r"\insertblocktitle")
    assert not band_violations(r"\vphantom{Ay}\insertblocktitle")
    assert hyphen_violations(r"\raggedright")
    assert not hyphen_violations(r"\hyphenpenalty=10000")
    assert boldmath_violations(r"\newcommand{\hl}[1]{\boldmath\textbf{#1}}")
    assert not boldmath_violations(r"\newcommand{\hl}[1]{{\boldmath\textbf{#1}}}")
    assert not boldmath_violations(
        r"\newcommand{\hlm}[1]{\textcolor{m}{\boldmath\textbf{#1}}}"
    )

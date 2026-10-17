"""Ratchet on hard-coded colours in the web UI — the count may only go down.

Dark mode works because every colour is a CSS variable with a light and a
dark value (``static/tailwind.src.css``) behind semantic classes
(``bg-surface``, ``text-fg-muted``, ``border-line``; ``tailwind.config.js``).
A hard-coded colour — a Tailwind palette class (``text-slate-500``) or a hex
or ``rgb()`` literal — paints the same in both schemes, so it is a spot dark
mode cannot reach. ``scripts/colour-codemod`` moved the UI onto tokens and
left the cases a machine cannot judge: JS and SVG-attribute colours,
gradient stops of data legends, non-palette hex.

The ceilings are the counts the codemod and its hand pass left (2026-10-17).
A new hard-coded colour fails here: use a token, or pin the box to the light
palette with ``.scheme-light`` if a script draws it in fixed colours.
Removing one leaves slack under the ceiling; lower the number in the same
commit so the slack doesn't accumulate. Raising a ceiling is a deliberate,
reviewed change.

Python modules and static JS that render HTML are scanned for palette
classes too (their hex values are SVG and canvas colours, out of scope): a
palette class there is also purged from ``tailwind.css`` once no template
uses it, so the element renders unstyled.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from tests._policy_scan import read_text, walk_files

REPO = Path(__file__).resolve().parents[2]
WEB = REPO / "src" / "precis_web"
STATIC = WEB / "static"
# Generated (tailwind.css) and the palette itself (tailwind.src.css).
NOT_SCANNED_CSS = {"tailwind.css", "tailwind.src.css"}

_HUES = (
    "slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|"
    "teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose"
)
PALETTE_CLASS = re.compile(
    r"(?<![\w/-])(?:[\w-]+:)*"
    r"(?:bg|text|fill|stroke|placeholder|divide|ring-offset|ring|outline|decoration"
    r"|accent|caret|shadow|from|via|to|border(?:-[xytblrse])?)"
    rf"-(?:(?:{_HUES})-\d{{2,3}}|white|black)(?:/\d{{1,3}})?(?![\w-])"
)
HEX = re.compile(
    r"(?<![\w&])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})(?![\w-])"
)
COLOUR_FN = re.compile(r"\b(?:rgba?|hsla?)\(\s*(?!var\()")
TEMPLATE_COMMENT = re.compile(r"\{#.*?#\}|<!--.*?-->", re.S)
CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)

CEILINGS = {
    "template palette classes": 0,
    "template hex and rgb() literals": 65,
    "css hex and rgb() literals": 14,
    "python and static js palette classes": 0,
}


def _scan() -> dict[str, Counter[str]]:
    hits: dict[str, Counter[str]] = {name: Counter() for name in CEILINGS}

    def rel(path: Path) -> str:
        return str(path.relative_to(REPO))

    for path in walk_files(WEB / "templates", "*.j2"):
        text = TEMPLATE_COMMENT.sub("", read_text(path))
        hits["template palette classes"][rel(path)] += len(PALETTE_CLASS.findall(text))
        hits["template hex and rgb() literals"][rel(path)] += len(
            HEX.findall(text)
        ) + len(COLOUR_FN.findall(text))
    for path in walk_files(STATIC, "*.css"):
        if path.parent != STATIC or path.name in NOT_SCANNED_CSS:
            continue
        text = CSS_COMMENT.sub("", read_text(path))
        hits["css hex and rgb() literals"][rel(path)] += len(HEX.findall(text)) + len(
            COLOUR_FN.findall(text)
        )
    code = walk_files(WEB, "*.py") + [
        p
        for p in walk_files(STATIC, "*.js")
        if p.parent == STATIC and not p.name.endswith(".min.js")
    ]
    for path in code:
        hits["python and static js palette classes"][rel(path)] += len(
            PALETTE_CLASS.findall(read_text(path))
        )
    return hits


def test_hard_coded_colours_never_grow() -> None:
    over = []
    for name, per_file in _scan().items():
        total = sum(per_file.values())
        if total > CEILINGS[name]:
            worst = ", ".join(f"{p} ({n})" for p, n in per_file.most_common(5) if n)
            over.append(f"{name}: {total} > ceiling {CEILINGS[name]} — most in {worst}")
    assert not over, (
        "hard-coded colours grew; use a semantic class or rgb(var(--token)) "
        "(src/precis_web/static/tailwind.src.css lists them), or `.scheme-light` "
        "around a script-drawn figure. Raising CEILINGS in "
        f"{Path(__file__).name} is the deliberate, reviewed route:\n  "
        + "\n  ".join(over)
    )


def test_the_patterns_see_what_they_count() -> None:
    """The scan is only a ratchet if its patterns hit the forms it guards."""
    sample = (
        'class="text-slate-500 hover:bg-emerald-50 border-t-rose-200/60 bg-white" '
        'style="color:#94a3b8;box-shadow:0 0 0 1px rgba(0,0,0,.2)"'
    )
    assert len(PALETTE_CLASS.findall(sample)) == 4
    assert HEX.findall(sample) == ["#94a3b8"]
    assert len(COLOUR_FN.findall(sample)) == 1
    tokens = 'class="text-fg-subtle bg-surface border-line text-teal-strong" &#9888; rgb(var(--x))'
    assert not PALETTE_CLASS.findall(tokens)
    assert not HEX.findall(tokens)
    assert not COLOUR_FN.findall(tokens)

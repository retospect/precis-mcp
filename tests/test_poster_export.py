"""Poster genre export — ``doc_type='poster'`` dispatches onto the packaged
``poster.tex``: profile roles injected as ``\\definecolor``, sections as
beamer ``block``s packed by ``meta.poster_column``, the logo strip written
into the build dir, and ``poster_profile.json`` recorded beside it."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.export import latex
from precis.export.poster_profile import PosterValidationError
from precis.handlers.draft import DraftHandler
from precis.handlers.figure import FigureHandler

# Big canvas so the resvg rasterisation (zoom 3) clears the 300-ppi floor
# at the default 4 cm render height: 800px viewBox → ~2400px PNG.
_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="800" '
    'viewBox="0 0 800 800"><rect width="800" height="800" fill="{hex}"/></svg>'
)


def _logo(hub: Hub, slug: str, hex_color: str) -> str:
    FigureHandler(hub=hub).put(id=slug, text=_SVG.format(hex=hex_color))
    return slug


def _poster_draft(hub: Hub, slug: str, poster: dict | None = None) -> int:
    ws: dict = {"path": f"projects/{slug}", "doc_type": "poster"}
    if poster is not None:
        ws["poster"] = poster
    project = hub.live_store.insert_ref(
        kind="todo", slug=None, title="P", meta={"workspace": ws}
    ).id
    d = DraftHandler(hub=hub)
    d.put(id=slug, title="Nanobuds", project=project, meta={"workspace": ws})
    d.edit(id=slug, scaffold="poster")
    ref = hub.live_store.get_ref(kind="draft", id=slug)
    assert ref is not None
    return ref.id


def _column(tex: str, name: str) -> str:
    """The ``\\PosterColumn<name>`` slot body — slots are emitted in order
    One, Two, Three before ``\\begin{document}``."""
    slots = ["One", "Two", "Three"]
    start = tex.index(f"\\renewcommand{{\\PosterColumn{name}}}{{")
    next_slot = min(
        (
            tex.index(f"\\renewcommand{{\\PosterColumn{s}}}{{")
            for s in slots
            if tex.index(f"\\renewcommand{{\\PosterColumn{s}}}{{") > start
        ),
        default=tex.index("\\begin{document}"),
    )
    return tex[start:next_slot]


def test_poster_export_uses_template_and_columns(hub: Hub, tmp_path: Path) -> None:
    ref_id = _poster_draft(hub, "po")
    d = DraftHandler(hub=hub)
    ro = hub.live_store.drafts.reading_order(ref_id)
    intro = next(c for c in ro if c.text == "Introduction")
    d.put(
        id="po",
        chunk_kind="paragraph",
        text="motivation text",
        at={"into": intro.dc, "last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="po")
    result = latex.export_draft(hub.live_store, ref, target_dir=tmp_path / "o")

    tex = result.main_tex.read_text(encoding="utf-8")
    assert "\\documentclass[final]{beamer}" in tex
    # column hints: Introduction + Methods in column 1, Results +
    # Discussion in 2, Conclusions + References in 3
    col1 = _column(tex, "One")
    assert "\\begin{block}{Introduction}" in col1
    assert "\\begin{block}{Methods}" in col1
    assert "motivation text" in col1
    assert "\\begin{block}{Results}" in _column(tex, "Two")
    assert "\\begin{block}{References}" in _column(tex, "Three")
    # product default palette injected
    assert "\\definecolor{accent}{HTML}{404040}" in tex
    assert result.poster_profile is not None and result.poster_profile.exists()
    saved = json.loads(result.poster_profile.read_text(encoding="utf-8"))
    assert saved["palette"]["accent"] == "#404040"
    # no strip without logos/sponsor line
    assert "\\renewcommand{\\PosterStrip}" not in tex


def test_two_profiles_differ_only_in_branding(hub: Hub, tmp_path: Path) -> None:
    logo_a = _logo(hub, "logo-a", "#005335")
    logo_b = _logo(hub, "logo-b", "#7B0046")
    profile_a = {
        "name": "ul",
        "palette": {"accent": "#005335", "band": "#003726"},
        "fonts": {"body": "lmodern"},
        "logos": [{"figure": logo_a, "alt": "UL"}],
    }
    profile_b = {
        "name": "mu",
        "palette": {"accent": "#7B0046", "band": "#5A0035"},
        "fonts": {"body": "helvet"},
        "logos": [{"figure": logo_b, "alt": "MU"}],
    }
    _poster_draft(hub, "pa", profile_a)
    _poster_draft(hub, "pb", profile_b)
    ra = latex.export_draft(
        hub.live_store,
        hub.live_store.get_ref(kind="draft", id="pa"),
        target_dir=tmp_path / "a",
    )
    rb = latex.export_draft(
        hub.live_store,
        hub.live_store.get_ref(kind="draft", id="pb"),
        target_dir=tmp_path / "b",
    )
    ta = ra.main_tex.read_text(encoding="utf-8")
    tb = rb.main_tex.read_text(encoding="utf-8")
    assert "\\definecolor{accent}{HTML}{005335}" in ta
    assert "\\definecolor{accent}{HTML}{7B0046}" in tb
    assert "\\usepackage{helvet}" in tb and "\\usepackage{helvet}" not in ta
    assert "logo01.png" in ta and "logo01.png" in tb
    # everything from \begin{document} on is identical — content, column
    # model and layout are profile-independent
    assert ta.split("\\begin{document}", 1)[1] == tb.split("\\begin{document}", 1)[1]
    # and the block content per column matches — only branding differs
    blocks_a = [
        re.findall(r"\\begin\{block\}\{([^}]*)\}", _column(ta, s))
        for s in ("One", "Two", "Three")
    ]
    blocks_b = [
        re.findall(r"\\begin\{block\}\{([^}]*)\}", _column(tb, s))
        for s in ("One", "Two", "Three")
    ]
    assert blocks_a == blocks_b
    assert (tmp_path / "a" / "logos" / "logo01.png").exists()
    assert (tmp_path / "b" / "logos" / "logo01.png").exists()


def test_sponsor_logo_and_line_reflow_strip(hub: Hub, tmp_path: Path) -> None:
    inst = _logo(hub, "inst", "#005335")
    spon = _logo(hub, "spon", "#222244")
    _poster_draft(
        hub,
        "sp",
        {
            "palette": {"accent": "#005335", "band": "#003726"},
            "logos": [
                {"figure": inst, "kind": "institution"},
                {"figure": spon, "kind": "sponsor", "height_cm": 2.5},
            ],
            "sponsor_line": "Funded by the Example Trust",
        },
    )
    ref = hub.live_store.get_ref(kind="draft", id="sp")
    result = latex.export_draft(hub.live_store, ref, target_dir=tmp_path / "o")
    tex = result.main_tex.read_text(encoding="utf-8")
    m = re.search(
        r"\\renewcommand\{\\PosterStrip\}\{\\PosterStripBox\{(.*)\}\}",
        tex,
        re.DOTALL,
    )
    assert m is not None
    strip = m.group(1)
    # institution first, sponsor second, sponsor line flushed right
    assert strip.index("logo01.png") < strip.index("logo02.png")
    assert "Funded by the Example Trust" in strip
    assert "\\hfill{\\large" in strip
    assert (tmp_path / "o" / "logos" / "logo01.png").exists()
    assert (tmp_path / "o" / "logos" / "logo02.png").exists()


def test_missing_logo_blocks_export(hub: Hub, tmp_path: Path) -> None:
    _poster_draft(hub, "ml", {"logos": [{"figure": "no-such-figure"}]})
    ref = hub.live_store.get_ref(kind="draft", id="ml")
    with pytest.raises(PosterValidationError) as ei:
        latex.export_draft(hub.live_store, ref, target_dir=tmp_path / "o")
    assert ei.value.issues[0].code == "poster_logo_missing"


def test_low_contrast_blocks_export(hub: Hub, tmp_path: Path) -> None:
    _poster_draft(
        hub,
        "lc",
        {"palette": {"band": "#111111", "bandtext": "#000000"}},
    )
    ref = hub.live_store.get_ref(kind="draft", id="lc")
    with pytest.raises(PosterValidationError) as ei:
        latex.export_draft(hub.live_store, ref, target_dir=tmp_path / "o")
    assert "poster_contrast_insufficient" in str(ei.value)

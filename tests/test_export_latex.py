"""LaTeX export for the draft kind (ADR 0033 Tier-B).

Pure-render unit tests for the inline converter / bib / acronym builders
(no DB), plus an end-to-end ``export_draft`` against real Postgres via
the ``hub`` fixture.
"""

from __future__ import annotations

import re

from precis.export import latex

# ── inline rendering (no DB) ──────────────────────────────────────────


def _ctx(text, abbrevs=None, known=None):
    """Build a render context. ``known_handles`` auto-includes every ¶
    handle in the text (so rendering tests don't trip the dangling-xref
    downgrade) unless the caller pins a set to exercise that path."""
    if known is None:
        known = set(re.findall(r"¶([A-Za-z0-9]+)", text))
    return latex._Ctx(keymap=latex._acronym_keymap(abbrevs or {}), known_handles=known)


def _inline(text, abbrevs=None, known=None):
    ctx = _ctx(text, abbrevs, known)
    out = latex._render_inline(text, ctx)
    return out, ctx


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


def test_cross_ref_and_citation() -> None:
    out, ctx = _inline("As shown in [¶abc123] and [§kong24~3] and paper:smith2024.")
    assert r"\cref{chunk:abc123}" in out
    assert r"\cite{kong24}" in out and r"\cite{smith2024}" in out
    assert ctx.cited == ["kong24", "smith2024"]


def test_dangling_cross_ref_downgrades() -> None:
    # abc123 is NOT a live handle → no \cref, surface text kept, warned.
    out, ctx = _inline("see [the intro](¶abc123) here", known=set())
    assert r"\cref" not in out and r"\hyperref" not in out
    assert "the intro" in out
    assert any("abc123" in w for w in ctx.warnings)


def test_display_link_and_url() -> None:
    out, _ = _inline("[the intro](¶abc123) and [DDG](https://duckduckgo.com)")
    assert r"\hyperref[chunk:abc123]{the intro}" in out
    assert r"\href{https://duckduckgo.com}{DDG}" in out


def test_authoring_link_renders_nothing() -> None:
    out, ctx = _inline("provenance [[memory:6184]] here")
    assert "memory" not in out and "6184" not in out
    assert ctx.cited == []


def test_unicode_translated_to_latex() -> None:
    out, _ = _inline("uptake ≈ 2.6 with α and →")
    assert "≈" not in out and "α" not in out  # non-ASCII gone
    assert r"\approx" in out and r"\alpha" in out


def test_acronym_keymap_dedups_collisions() -> None:
    km = latex._acronym_keymap({"PEI": "polyethyleneimine", "P.E.I.": "place"})
    # both sanitise to "pei"; the map keeps them distinct
    assert km["PEI"] != km["P.E.I."]
    assert len(set(km.values())) == 2


def test_glsify_known_abbrev() -> None:
    out, _ = _inline("We graft PEI; PEINE differs.", {"PEI": "polyethyleneimine"})
    assert r"\gls{pei}" in out
    assert "PEINE" in out  # not a whole-word PEI


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
    title_h = store.reading_order(ref.id)[0].handle

    draft.put(
        id="nt", chunk_kind="heading", text="Introduction", at={"after": f"¶{title_h}"}
    )
    sec_h = next(
        c.handle for c in store.reading_order(ref.id) if c.text == "Introduction"
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
    main = result.main_tex.read_text()

    assert (tmp_path / "out" / "preamble.tex").exists()
    assert r"\documentclass" in main and r"\begin{document}" in main
    assert r"\title{Nanoscale Transistors}" in main
    assert r"\section{Introduction}\label{chunk:" in main
    assert r"\newacronym{pei}{PEI}{polyethyleneimine}" in main
    assert r"\gls{pei}" in main  # surface occurrence glsified
    assert r"\printglossaries" in main and r"\printbibliography" in main
    # the Glossary heading + term chunk are NOT rendered as body sections
    assert r"\section{Glossary}" not in main


# ── compile (stub latexmk) ────────────────────────────────────────────


def _stub_latexmk(tmp_path, *, succeed=True):
    """A fake latexmk that touches main.pdf (or not) and exits 0/1 —
    lets us exercise compile_pdf without a TeX install (PRECIS_LATEXMK_BIN
    mirrors the PRECIS_CLAUDE_BIN stub-binary pattern)."""
    import os
    import stat

    script = tmp_path / "latexmk"
    body = "#!/bin/sh\n"
    body += (
        "touch main.pdf\nexit 0\n" if succeed else "echo '! Undefined.' >&2\nexit 1\n"
    )
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    os.environ["PRECIS_LATEXMK_BIN"] = str(script)
    return script


def test_compile_pdf_success(tmp_path, monkeypatch) -> None:
    from precis.export import compile as cmpl

    monkeypatch.setenv("PRECIS_LATEXMK_BIN", str(_stub_latexmk(tmp_path)))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}x\\end{document}"
    )
    res = cmpl.compile_pdf(proj)
    assert res.ok and res.pdf == proj / "main.pdf" and res.pdf.exists()


def test_compile_pdf_failure_returns_log(tmp_path, monkeypatch) -> None:
    from precis.export import compile as cmpl

    monkeypatch.setenv(
        "PRECIS_LATEXMK_BIN", str(_stub_latexmk(tmp_path, succeed=False))
    )
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "main.tex").write_text("broken")
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
    assert "makeglossaries" in result.latexmkrc.read_text()

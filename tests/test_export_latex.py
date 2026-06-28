"""LaTeX export for the draft kind (ADR 0033 Tier-B).

Pure-render unit tests for the inline converter / bib / acronym builders
(no DB), plus an end-to-end ``export_draft`` against real Postgres via
the ``hub`` fixture.
"""

from __future__ import annotations

import re

from precis.export import latex

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
    assert "$Zr_6$" in out
    assert "$UO_2^{2+}$" in out
    assert "$W_{18}$$O_{49}$" in out


def test_paper_handle_renders_citation() -> None:
    # ADR 0036: a paper handle [pc10] / [pa99] → \cite via the cite_key.
    out, ctx = _inline("see [pc10] here", store=_PaperStore())
    assert r"\cite{kong24}" in out and ctx.cited == ["kong24"]


def test_record_handle_renders_nothing() -> None:
    # A thought handle [me5] is provenance-only in export — dropped.
    out, ctx = _inline("aside [me5] here")
    assert "me5" not in out and ctx.cited == []


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
    """A plural surface (MOFs) links to the same term via \\glspl — so MOF
    and MOFs share one glossary entry rather than leaving the plural bare."""
    out, _ = _inline("one MOF, several MOFs.", {"MOF": "metal-organic framework"})
    assert r"\gls{mof}" in out
    assert r"\glspl{mof}" in out
    assert "MOFs" not in out  # the plural was absorbed, not left literal


def test_glsify_plural_no_false_match() -> None:
    """A trailing-s word that merely starts with a short is left alone."""
    out, _ = _inline("DNase activity in DNA.", {"DNA": "deoxyribonucleic acid"})
    assert r"\gls{dna}" in out
    assert "DNase" in out  # not glspl{dna}


def test_handle_xref_dc_renders_cref() -> None:
    """ADR 0036 single-bracket [dc<id>] -> \\cref to the in-draft chunk."""
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
    with a direct cite of that paper), else its pub_id placeholder."""
    import types

    established = types.SimpleNamespace(meta={"primary_cite_key": "miller23"})
    inflight = types.SimpleNamespace(meta={"pub_id": "ab12c3"})
    store = types.SimpleNamespace(
        fetch_refs_by_ids=lambda ids: (
            {7: established} if 7 in ids else {9: inflight} if 9 in ids else {}
        )
    )
    ctx = latex._Ctx(keymap={}, known_handles=set(), store=store)
    out = latex._render_inline("est [fi7], inflight [fi9].", ctx)
    assert r"\cite{miller23}" in out  # established → primary cite_key
    assert r"\cite{ab12c3}" in out  # in-flight → pub_id placeholder


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

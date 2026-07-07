"""LaTeX export for the draft kind (ADR 0033 Tier-B).

Pure-render unit tests for the inline converter / bib / acronym builders
(no DB), plus an end-to-end ``export_draft`` against real Postgres via
the ``hub`` fixture.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from precis.export import latex

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
    main = result.main_tex.read_text()
    assert r"\includepdf[pages=-]{sources/smith2020.pdf}" in main
    assert result.source_bundle is bundle


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


@_needs_posix_stub
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


@_needs_posix_stub
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


def test_export_renders_table_as_longtable(hub, tmp_path) -> None:
    """A chunk_kind='table' renders as a booktabs longtable (ADR 0035 §1)
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
        assert "\\author[1]{Doe, Jane}" in out
        assert "\\author[2]{Roe, John}" in out
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
    assert "\\author[1]{Doe, Jane}" in main_tex
    assert "\\affil[1]{\\href{https://ror.org/x}{MIT}}" in main_tex
    assert "\\affil[2]{Caltech}" in main_tex

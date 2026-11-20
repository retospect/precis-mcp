"""Draft → .docx export — structure, inline formatting, citation
integrity (numbered references resolved through the shared paper lookup),
and the glossary. Round-trips through python-docx, which is itself a
validity check (a corrupt part would fail to re-open)."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler
from precis.handlers.todo import TodoHandler
from precis.store import ChunkInsert, Store

docx = pytest.importorskip("docx")  # python-docx (the `docx` extra)

from precis.export.docx import export_docx


def _seed_paper(store: Store, slug: str, title: str, year: int) -> None:
    store.insert_ref(kind="paper", slug=slug, title=title, year=year, provider="manual")
    paper_ref = store.get_ref(kind="paper", id=slug)
    assert paper_ref is not None
    store.chunks.insert_chunks(
        paper_ref.id,
        [ChunkInsert(ord=0, text="body", slug="b0")],
    )


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _make_draft(draft: DraftHandler, hub: Hub) -> object:
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="d1", title="CO2 Capture in MOFs", project=pid)
    sec = draft.put(id="d1", chunk_kind="heading", text="Methods", at={"last": True})
    import re

    sec_h = re.search(r"dc\d+", sec.body).group(0)  # type: ignore[union-attr]
    draft.put(
        id="d1",
        chunk_kind="paragraph",
        text="Amine **functionalization** improves uptake [§miller2020~0].",
        at={"into": sec_h, "last": True},
    )
    draft.put(
        id="d1",
        chunk_kind="term",
        text="metal-organic framework",
        meta={"short": "MOF"},
    )
    return hub.live_store.get_ref(kind="draft", id="d1")


def test_export_produces_valid_docx(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    _seed_paper(hub.live_store, "miller2020", "A study of MOFs", 2020)
    ref = _make_draft(draft, hub)
    out = tmp_path / "d1.docx"
    res = export_docx(hub.live_store, ref, target_path=out)
    assert out.is_file()
    # Re-open (validity check) and read the text.
    doc = docx.Document(str(out))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "CO2 Capture in MOFs" in text  # title
    assert "Methods" in text  # heading
    assert "functionalization" in text  # bold run content
    # Citation renders as a numbered superscript marker in the prose, backed
    # by the References section (no native endnote field — those can't repeat).
    assert "[1]" in text
    import zipfile

    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        body = z.read("word/document.xml").decode("utf-8")
    assert "word/endnotes.xml" not in names  # no endnote part
    assert "endnoteReference" not in body
    # The [1] mark is a superscript run.
    assert '<w:vertAlign w:val="superscript"/>' in body


def test_citation_integrity_in_references(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    _seed_paper(hub.live_store, "miller2020", "A study of MOFs", 2020)
    ref = _make_draft(draft, hub)
    out = tmp_path / "d1.docx"
    res = export_docx(hub.live_store, ref, target_path=out)
    assert res.cited_slugs == ["miller2020"]
    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    # A numbered References section carries the entry resolved through the
    # SAME paper lookup as the .bib path (integrity parity with the PDF).
    assert "References" in text
    assert "[1]" in text  # entry number == in-text mark
    assert "A study of MOFs" in text  # resolved title
    assert "2020" in text


def test_repeated_citation_does_not_corrupt(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """A paper cited at *non-adjacent* sites prints ``[1]`` each time and
    yields ONE References entry — the case a native Word endnote can't model
    (an endnote is 1:1 with its reference, and reusing one makes Word declare
    the document's content unreadable)."""
    _seed_paper(hub.live_store, "wu22", "Wu 2022 study", 2022)
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="dr", title="T", project=pid)
    draft.put(
        id="dr",
        chunk_kind="paragraph",
        text="First claim [§wu22~3]. Then unrelated prose. Second claim [§wu22~9].",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="dr")
    out = tmp_path / "dr.docx"
    res = export_docx(hub.live_store, ref, target_path=out)
    assert res.cited_slugs == ["wu22"]
    import zipfile

    with zipfile.ZipFile(out) as z:
        body = z.read("word/document.xml").decode("utf-8")
    # Two non-adjacent marks → the same number reused, no endnote machinery.
    assert body.count("endnoteReference") == 0
    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    assert text.count("[1]") >= 2  # the mark repeats (≥2 sites + References)
    assert "Wu 2022 study" in text  # one resolved entry


def test_docx_embeds_figure_image(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """A canvas figure rasterises to PNG and embeds as an inline image + an
    italic caption paragraph. Uses a canvas (SVG→PNG via
    resvg) so the embedded raster is one python-docx accepts."""
    pytest.importorskip("resvg_py")
    from precis.handlers.figure import FigureHandler

    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="d1", title="T", project=pid)
    ref = hub.live_store.get_ref(kind="draft", id="d1")
    assert ref is not None
    title_h = hub.live_store.drafts.reading_order(ref.id)[0].handle
    # a caption-only figure chunk (no blob) backed by a canvas.
    fig = hub.live_store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="figure",
        text="Fig 1. A widget.",
        at={"after": title_h},
        split=False,
    )[0]
    FigureHandler(hub=hub).put(
        id="fig1",
        title="Widget",
        text=(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 60">'
            '<rect x="5" y="5" width="90" height="50" fill="none" stroke="black"/>'
            "</svg>"
        ),
    )
    canvas = hub.live_store.get_ref(kind="figure", id="fig1")
    assert canvas is not None
    hub.live_store.drafts.link_figure_canvas(fig.chunk_id, canvas.id)

    out = tmp_path / "d1.docx"
    res = export_docx(hub.live_store, ref, target_path=out)

    doc = docx.Document(str(out))
    assert len(doc.inline_shapes) == 1  # the rasterised canvas embedded
    # alt text from the caption; a vector source the exporter rasterised is
    # exempt from the resolution check (gr725249)
    assert doc.inline_shapes[0]._inline.docPr.get("descr") == "Fig 1. A widget."
    assert not any("dpi" in w for w in res.warnings)
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "Fig 1. A widget." in text  # caption


def test_docx_withheld_figure_is_boxed_not_embedded(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """A handle in ``withheld_figures`` renders a bordered notice naming the
    publisher and source plus the caption; the image is never embedded."""
    import base64

    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="d1", title="T", project=pid)
    ref = hub.live_store.get_ref(kind="draft", id="d1")
    assert ref is not None
    title_h = hub.live_store.drafts.reading_order(ref.id)[0].handle
    # 1x1 PNG
    png = base64.b64encode(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhE"
            + "UgAAAAEAAAABCAYAAAAf"
            + "FcSJAAAADUlEQVR42mNk"
            + "YPhfDwAChwGA60e6kgAA"
            + "AABJRU5ErkJggg=="
        )
    ).decode()
    draft.put(
        id="d1",
        chunk_kind="figure",
        text="Fig 1. Borrowed.",
        image=png,
        origin="third_party",
        permission={
            "publisher": "ACME",
            "source_paper": "Doe 2020",
            "status": "requested",
        },
        at={"after": f"\u00b6{title_h}"},
    )
    figc = next(
        c
        for c in hub.live_store.drafts.reading_order(ref.id)
        if c.chunk_kind == "figure"
    )
    out = tmp_path / "d1.docx"
    export_docx(
        hub.live_store, ref, target_path=out, withheld_figures=frozenset({figc.dc})
    )
    doc = docx.Document(str(out))
    assert len(doc.inline_shapes) == 0
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "Figure withheld pending permission: ACME, Doe 2020" in text
    assert "Fig 1. Borrowed." in text


def test_glossary_terms_become_a_sorted_used_only_acronym_list(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """The draft's own Glossary heading + term chunks are not body; the back
    matter is an "Acronyms" list of the shorts the prose used, sorted
    case-insensitively (LaTeX's generated acronym list)."""
    pid = _new_draft_project(hub)
    draft.put(id="dgl", title="T", project=pid)
    for short, long in [
        ("pG", "pristine graphene"),
        ("CNT", "carbon nanotube"),
        ("DFT", "density functional theory"),  # defined, never used
        ("ha", "hexylamine"),
    ]:
        draft.put(id="dgl", chunk_kind="term", text=long, meta={"short": short})
    draft.put(
        id="dgl",
        chunk_kind="paragraph",
        text="We study pG, ha and CNT here.",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="dgl")
    out = tmp_path / "dgl.docx"
    export_docx(hub.live_store, ref, target_path=out)
    doc = docx.Document(str(out))
    texts = [p.text for p in doc.paragraphs]
    assert "Glossary" not in texts
    i = texts.index("Acronyms")
    assert texts[i + 1 :] == [
        "CNT: carbon nanotube",
        "ha: hexylamine",
        "pG: pristine graphene",
    ]
    assert not any(t.startswith("DFT") for t in texts)  # never used in the body
    # The look lives in two styles, not in direct run formatting.
    entry = doc.paragraphs[i + 1]
    assert entry.style.name == "Definition"
    assert entry.runs[0].text == "CNT: "
    assert entry.runs[0].style.name == "Definition Term"
    assert all(r.bold is None for r in entry.runs)
    assert doc.styles["Definition Term"].font.bold is True


def test_missing_paper_warns_but_exports(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    ref = _make_draft(draft, hub)  # cites miller2020 which is NOT seeded
    out = tmp_path / "d1.docx"
    res = export_docx(hub.live_store, ref, target_path=out)
    assert out.is_file()
    assert any("miller2020" in w for w in res.warnings)


def test_acronym_first_use_expansion(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="dx", title="T", project=pid)
    draft.put(
        id="dx",
        chunk_kind="term",
        text="metal-organic framework",
        meta={"short": "MOF"},
    )
    draft.put(
        id="dx",
        chunk_kind="paragraph",
        text="First, MOF systems. Later, more MOFs appear.",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="dx")
    out = tmp_path / "dx.docx"
    export_docx(hub.live_store, ref, target_path=out)
    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    # First prose occurrence expanded; later plural stays abbreviated.
    assert "metal-organic framework (MOF)" in text
    assert "MOFs appear" in text  # plural, not expanded
    # The used abbreviation lands in the generated Acronyms list.
    assert "Acronyms" in text
    assert "MOF: metal-organic framework" in text
    assert "Glossary" not in text


def test_math_renders_as_omml(draft: DraftHandler, hub: Hub, tmp_path: Path) -> None:
    pytest.importorskip("latex2mathml")
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="dm", title="T", project=pid)
    draft.put(
        id="dm",
        chunk_kind="paragraph",
        text="The relation $E = mc^2$ and a fraction $\\frac{a}{b}$.",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="dm")
    out = tmp_path / "dm.docx"
    export_docx(hub.live_store, ref, target_path=out)
    # Inspect the document XML for native OMML math.
    import zipfile

    with zipfile.ZipFile(out) as z:
        doc_xml = z.read("word/document.xml").decode("utf-8")
    assert "oMath" in doc_xml  # native math, not literal "$...$"
    assert "sSup" in doc_xml  # the c^2 superscript
    assert "}f" in doc_xml or "<m:f" in doc_xml or ":f>" in doc_xml  # the fraction
    assert "$" not in doc_xml  # no leftover literal math source
    # Re-open as a validity check.
    docx.Document(str(out))


def test_garbled_and_money_math_demoted_to_literal_text(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    r"""Parity with the LaTeX exporter's demotion predicates
    (``_math_braces_balanced`` / ``_math_plausible``, shared via import):
    an unbalanced-brace span and a money-dollar mispairing render as
    literal prose runs, never OMML; an author-escaped ``\$`` renders as
    the bare ``$`` (Word has no escape syntax)."""
    pytest.importorskip("latex2mathml")
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="dg", title="T", project=pid)
    draft.put(
        id="dg",
        chunk_kind="paragraph",
        text=(
            r"The ratio $\sqrt{2/\sqrt{3}$ holds. Oligomers cost "
            r"$10-50 each, versus $200 for staples, plus \$99 fees."
        ),
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="dg")
    out = tmp_path / "dg.docx"
    export_docx(hub.live_store, ref, target_path=out)
    import zipfile

    with zipfile.ZipFile(out) as z:
        doc_xml = z.read("word/document.xml").decode("utf-8")
    assert "oMath" not in doc_xml  # nothing here is math
    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    assert r"$\sqrt{2/\sqrt{3}$" in text  # demoted verbatim, not OMML
    assert "$10-50 each, versus $200 for staples" in text
    assert "$99 fees" in text and r"\$" not in text  # \$ → bare $


def test_empty_base_math_gets_a_base(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """``Zr$_6$`` / ``UO$_2^{2+}$`` put the base outside the math, leaving an
    empty subscript base — an empty OMML ``<m:e/>`` Word draws as a dotted-box
    placeholder. The exporter folds the adjacent token into the math instead."""
    pytest.importorskip("latex2mathml")
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="dz", title="T", project=pid)
    draft.put(
        id="dz",
        chunk_kind="paragraph",
        text="The Zr$_6$ node and the UO$_2^{2+}$ ion.",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="dz")
    out = tmp_path / "dz.docx"
    export_docx(hub.live_store, ref, target_path=out)
    import zipfile

    with zipfile.ZipFile(out) as z:
        doc_xml = z.read("word/document.xml").decode("utf-8")
    assert "<m:e/>" not in doc_xml  # no empty base → no dotted box
    assert "oMath" in doc_xml
    docx.Document(str(out))


def test_standalone_equation_numbered_and_cross_referenced(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """A paragraph chunk whose ENTIRE text is one ``$$…$$`` span numbers
    "(N)" next to the native OMML math, and a bare ``[dc<id>]`` cross-ref
    to that chunk auto-resolves to "Eq. (N)" — no new authoring syntax.
    docx has no live cross-reference field (unlike the PDF path's
    cleveref), so the number is a static count resolved at export time."""
    pytest.importorskip("latex2mathml")
    import re

    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="eqd", title="T", project=pid)
    eq1 = draft.put(
        id="eqd", chunk_kind="paragraph", text="$$E = mc^2$$", at={"last": True}
    )
    dc1 = re.search(r"dc\d+", eq1.body).group(0)  # type: ignore[union-attr]
    draft.put(
        id="eqd",
        chunk_kind="paragraph",
        text=f"As shown in [{dc1}], mass and energy relate.",
        at={"last": True},
    )
    draft.put(id="eqd", chunk_kind="paragraph", text="$$F = ma$$", at={"last": True})

    ref = hub.live_store.get_ref(kind="draft", id="eqd")
    out = tmp_path / "eqd.docx"
    export_docx(hub.live_store, ref, target_path=out)
    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    assert "(1)" in text and "(2)" in text  # the two equations, numbered in order
    assert "Eq. (1)" in text  # the bare [dc<id>] cross-ref auto-resolved
    import zipfile

    with zipfile.ZipFile(out) as z:
        doc_xml = z.read("word/document.xml").decode("utf-8")
    assert doc_xml.count("oMath") >= 4  # two equations, each open+close tag


def test_starred_equation_is_unnumbered_in_docx(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """A trailing ``*`` right after the closing ``$$`` opts a display
    equation out of numbering — no "(N)" label, and the marker itself
    never leaks into the rendered text."""
    pytest.importorskip("latex2mathml")
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="eqs", title="T", project=pid)
    draft.put(
        id="eqs",
        chunk_kind="paragraph",
        text="$$a^2 + b^2 = c^2$$*",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="eqs")
    out = tmp_path / "eqs.docx"
    export_docx(hub.live_store, ref, target_path=out)
    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    assert "(1)" not in text
    assert "*" not in text  # the star marker itself never leaks into output
    import zipfile

    with zipfile.ZipFile(out) as z:
        doc_xml = z.read("word/document.xml").decode("utf-8")
    assert "oMath" in doc_xml


def test_latex_cite_command_is_folded(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """A draft carrying verbatim LaTeX ``\\cite{key}`` resolves to a numbered
    mark + References entry — the ``\\cite{`` / ``}`` wrapper never leaks as
    literal text."""
    _seed_paper(hub.live_store, "wu22", "Wu 2022 study", 2022)
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="dc", title="T", project=pid)
    draft.put(
        id="dc",
        chunk_kind="paragraph",
        text="Adsorption is fast \\cite{wu22}.",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="dc")
    out = tmp_path / "dc.docx"
    res = export_docx(hub.live_store, ref, target_path=out)
    assert res.cited_slugs == ["wu22"]
    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    assert "\\cite" not in text and "cite{" not in text  # wrapper folded away
    assert "[1]" in text  # resolved to a numbered mark
    assert "Wu 2022 study" in text  # References entry


def test_handle_form_citation_resolves(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """A draft that cites a paper by universal handle (``[pa<ref_id>]``, the
    form every LaTeX-imported draft uses) must produce a numbered mark +
    References entry — NOT render as nothing. Regression: the docx exporter
    used to drop handle citations entirely (the LaTeX/PDF path resolved them),
    so handle-cited drafts exported with no citations and no References
    section at all."""
    from precis.utils import handle_registry

    _seed_paper(hub.live_store, "nasibulin2007", "Multifunctional nanobuds", 2007)
    pref = hub.live_store.get_ref(kind="paper", id="nasibulin2007")
    assert pref is not None
    handle = handle_registry.format_handle("paper", pref.id)  # 'pa<ref_id>'
    assert handle.startswith("pa")
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="dh", title="T", project=pid)
    draft.put(
        id="dh",
        chunk_kind="paragraph",
        text=f"Nanobuds were first reported [{handle}].",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="dh")
    out = tmp_path / "dh.docx"
    res = export_docx(hub.live_store, ref, target_path=out)
    assert res.cited_slugs == ["nasibulin2007"]  # handle resolved to the slug
    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    assert "[1]" in text  # numbered mark emitted (not dropped)
    assert "References" in text
    assert "Multifunctional nanobuds" in text  # resolved entry


def test_computed_evidence_handle_renders_as_text(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """A bracket-handle cite of a computational-evidence kind (a simulation
    structure, ``[st<id>]``) is real grounding, not a bibliography entry —
    it must render as plain text (handle, or display-form surface text)
    rather than being silently dropped."""
    from precis.utils import handle_registry

    art = hub.live_store.insert_ref(
        kind="structure", slug="pd-relaxed", title="Pd relaxed cell", meta={}
    )
    handle = handle_registry.format_handle("structure", art.id)  # 'st<ref_id>'
    assert handle.startswith("st")
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="dc-evi", title="T", project=pid)
    draft.put(
        id="dc-evi",
        chunk_kind="paragraph",
        text=(
            f"The relaxed geometry [{handle}] and the "
            f"[computed relaxation]({handle}) confirm the bond length."
        ),
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="dc-evi")
    out = tmp_path / "dc-evi.docx"
    export_docx(hub.live_store, ref, target_path=out)
    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    assert handle in text  # bare bracket handle rendered as plain text
    assert "computed relaxation" in text  # display-form surface text kept


def test_endnote_citations_emit_cwyw_fields(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """``citations='endnote'`` emits native EndNote Cite-While-You-Write
    fields (``ADDIN EN.CITE`` carrying the full <record>, plus an
    ``EN.REFLIST`` bibliography field and the EN.* doc-vars) instead of a
    plain ``[n]`` marker — the format EndNote recognizes and can reformat.
    Structure pinned against a real EndNote-authored .docx."""
    import zipfile

    _seed_paper(hub.live_store, "nasibulin2007", "Multifunctional nanobuds", 2007)
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="en", title="T", project=pid)
    draft.put(
        id="en",
        chunk_kind="paragraph",
        text="Nanobuds were first reported [§nasibulin2007~0].",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="en")
    out = tmp_path / "en.docx"
    res = export_docx(hub.live_store, ref, target_path=out, citations="endnote")
    assert res.cited_slugs == ["nasibulin2007"]
    with zipfile.ZipFile(out) as z:
        body = z.read("word/document.xml").decode("utf-8")
        settings = z.read("word/settings.xml").decode("utf-8")
    # In-text EN.CITE complex field carrying the traveling-library record.
    assert " ADDIN EN.CITE &lt;EndNote&gt;&lt;Cite&gt;" in body
    assert '&lt;ref-type name="Journal Article"&gt;17' in body
    assert "Multifunctional nanobuds" in body  # embedded record title
    assert '<w:fldChar w:fldCharType="begin"/>' in body
    # Bibliography field + the document-level EndNote state EndNote reads.
    assert " ADDIN EN.REFLIST " in body
    assert "EN.InstantFormat" in settings
    assert "EN.Layout" in settings
    assert "EN.Libraries" in settings
    # Re-open as a validity check (a malformed part would fail here).
    docx.Document(str(out))
    # Plain mode is unchanged — no EndNote machinery leaks in.
    plain = tmp_path / "plain.docx"
    export_docx(hub.live_store, ref, target_path=plain, citations="plain")
    with zipfile.ZipFile(plain) as z:
        pbody = z.read("word/document.xml").decode("utf-8")
    assert "ADDIN EN.CITE" not in pbody
    assert "[1]" in "\n".join(p.text for p in docx.Document(str(plain)).paragraphs)


def test_endnote_pc_citation_embeds_cited_passage(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """A ``[pc<id>]`` chunk citation embeds that chunk's text as the EndNote
    record's ``<research-notes>`` (the traveling note) — so the exact cited
    passage rides along. A ``[pa<id>]`` ref-level cite (whole paper) carries
    no passage."""
    import zipfile

    store = hub.live_store
    store.insert_ref(
        kind="paper", slug="nas07", title="Nanobuds paper", year=2007, provider="manual"
    )
    pref = store.get_ref(kind="paper", id="nas07")
    assert pref is not None
    passage = "The exact cited passage about sp3 rehybridization at junctions."
    store.chunks.insert_chunks(pref.id, [ChunkInsert(ord=0, text=passage, slug="b0")])
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "ORDER BY ord LIMIT 1",
            (pref.id,),
        ).fetchone()
        assert row is not None
        chunk_id = int(row[0])
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="pc", title="T", project=pid)
    draft.put(
        id="pc",
        chunk_kind="paragraph",
        text=f"Junctions rehybridize [pc{chunk_id}].",
        at={"last": True},
    )
    ref = store.get_ref(kind="draft", id="pc")
    out = tmp_path / "pc.docx"
    res = export_docx(store, ref, target_path=out, citations="endnote")
    assert res.cited_slugs == ["nas07"]
    body = zipfile.ZipFile(out).read("word/document.xml").decode("utf-8")
    # The cited chunk's text is embedded as the record's Research Notes.
    assert "&lt;research-notes&gt;" in body
    assert "sp3 rehybridization at junctions" in body  # the passage rode along
    docx.Document(str(out))  # validity


def test_adjacent_citations_merge_one_endnote_field(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """A run of directly adjacent citations becomes ONE ``ADDIN EN.CITE``
    field carrying several ``<Cite>`` elements — exactly how EndNote itself
    writes a grouped citation (gr491598). Spaced-apart cites keep their own
    fields, and a single cite stays one ``<Cite>``."""
    import zipfile

    store = hub.live_store
    _seed_paper(store, "p1", "Paper one", 2001)
    _seed_paper(store, "p2", "Paper two", 2002)
    _seed_paper(store, "p3", "Paper three", 2003)
    _seed_paper(store, "p4", "Paper four", 2004)
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="adj", title="T", project=pid)
    for text in (
        "Merged [§p1][§p2] pair.",
        "Spaced [§p1] [§p2] apart.",
        "Solo [§p4]",
    ):
        draft.put(id="adj", chunk_kind="paragraph", text=text, at={"last": True})
    ref = store.get_ref(kind="draft", id="adj")
    out = tmp_path / "adj.docx"
    export_docx(store, ref, target_path=out, citations="endnote")
    body = zipfile.ZipFile(out).read("word/document.xml").decode("utf-8")
    # Fields: 1 merged (2 cites) + 2 spaced singles + 1 solo = 4.
    assert body.count(" ADDIN EN.CITE ") == 4
    # <Cite> elements: 2 + 1 + 1 + 1 = 5, the merged field carrying two
    # back-to-back.
    assert body.count("&lt;Cite&gt;") == 5
    assert "&lt;/Cite&gt;&lt;Cite&gt;" in body
    docx.Document(str(out))  # validity


def test_adjacent_citations_merge_plain_mark(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """Plain (non-field) mode renders the same adjacent run merged —
    ``[1, 2]``, a run of ≥3 consecutive numbers ``[1–3]`` — mirroring
    ``export/latex.py::_merge_adjacent_cites`` (biblatex numeric-comp).
    Whitespace-separated cites stay separate; a single cite is unchanged."""
    store = hub.live_store
    _seed_paper(store, "p1", "Paper one", 2001)
    _seed_paper(store, "p2", "Paper two", 2002)
    _seed_paper(store, "p3", "Paper three", 2003)
    _seed_paper(store, "p4", "Paper four", 2004)
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="pm", title="T", project=pid)
    for text in (
        "Pair [§p1][§p2] here.",
        "Triple [§p1][§p2][§p3] run.",
        "Spaced [§p1] [§p2] apart.",
        "Solo [§p4]",
    ):
        draft.put(id="pm", chunk_kind="paragraph", text=text, at={"last": True})
    ref = store.get_ref(kind="draft", id="pm")
    out = tmp_path / "pm.docx"
    res = export_docx(store, ref, target_path=out)
    assert res.cited_slugs == ["p1", "p2", "p3", "p4"]
    paras = [p.text for p in docx.Document(str(out)).paragraphs]
    pair = next(t for t in paras if t.startswith("Pair"))
    assert "[1, 2]" in pair and "[1]" not in pair
    triple = next(t for t in paras if t.startswith("Triple"))
    assert "[1–3]" in triple
    spaced = next(t for t in paras if t.startswith("Spaced"))
    assert "[1] [2]" in spaced and "[1, 2]" not in spaced
    solo = next(t for t in paras if t.startswith("Solo"))
    assert solo == "Solo [4]"


def test_omml_converter_returns_none_on_empty() -> None:
    pytest.importorskip("latex2mathml")
    from precis.export.omml import latex_to_omml

    assert latex_to_omml("") is None
    assert latex_to_omml("   ") is None
    el = latex_to_omml("x^2")  # well-formed → an <m:oMath> element
    assert el is not None and el.tag.endswith("oMath")


def test_same_paper_chunks_collapse_to_one_mark(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """a~3, a~9, a~23 (different chunks, same paper) → ONE References entry,
    and consecutive marks collapse to a single ``[1]``."""
    _seed_paper(hub.live_store, "wu22", "Wu 2022 study", 2022)
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="dd", title="T", project=pid)
    draft.put(
        id="dd",
        chunk_kind="paragraph",
        text="Several findings [§wu22~3] [§wu22~9] [§wu22~23] agree.",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="dd")
    out = tmp_path / "dd.docx"
    res = export_docx(hub.live_store, ref, target_path=out)
    assert res.cited_slugs == ["wu22"]  # one paper, deduped
    paras = [p.text for p in docx.Document(str(out)).paragraphs]
    body_para = next(p for p in paras if "Several findings" in p)
    assert body_para.count("[1]") == 1  # consecutive marks collapsed to one
    assert "Wu 2022 study" in "\n".join(paras)  # one References entry


def test_export_renders_list_styles(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """ulist/olist items get Word's built-in List Bullet / List Number
    styles; the container itself emits no paragraph (migration 0037)."""
    import re

    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="ld", title="Lists", project=pid)
    ul = draft.put(id="ld", chunk_kind="ulist", text="list", at={"last": True})
    ul_h = re.search(r"dc\d+", ul.body).group(0)  # type: ignore[union-attr]
    draft.put(id="ld", chunk_kind="item", text="alpha", at={"into": ul_h, "last": True})
    ol = draft.put(id="ld", chunk_kind="olist", text="list", at={"last": True})
    ol_h = re.search(r"dc\d+", ol.body).group(0)  # type: ignore[union-attr]
    draft.put(id="ld", chunk_kind="item", text="one", at={"into": ol_h, "last": True})

    ref = hub.live_store.get_ref(kind="draft", id="ld")
    out = tmp_path / "ld.docx"
    export_docx(hub.live_store, ref, target_path=out)
    doc = docx.Document(str(out))
    styled = {
        p.text: p.style.name for p in doc.paragraphs if p.text in ("alpha", "one")
    }
    assert styled.get("alpha") == "List Bullet"
    assert styled.get("one") == "List Number"


def test_markdown_bullets_export_as_nested_list_styles(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """End to end, the trap this closes: bullet text written into a
    paragraph used to reach Word as one run-on Body Text line. It now
    lands structured (:mod:`precis.draft.mdlist`), so the nesting level
    picks List Bullet 2."""
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="md", title="Lists", project=pid)
    draft.put(
        id="md",
        chunk_kind="paragraph",
        text="- NO side\n    - Bader charge shows 0.39 e\n- NH3 side",
        at={"last": True},
    )

    ref = hub.live_store.get_ref(kind="draft", id="md")
    out = tmp_path / "md.docx"
    export_docx(hub.live_store, ref, target_path=out)
    doc = docx.Document(str(out))
    styled = {p.text: p.style.name for p in doc.paragraphs if p.text}
    assert styled.get("NO side") == "List Bullet"
    assert styled.get("NH3 side") == "List Bullet"
    assert styled.get("Bader charge shows 0.39 e") == "List Bullet 2"


def test_export_renders_table(draft: DraftHandler, hub: Hub, tmp_path: Path) -> None:
    """A chunk_kind='table' becomes a native Word table:
    header row bold, one body row per data row, cells via the inline grammar.
    The derived pipe markdown is not dumped as a paragraph."""
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="tb", title="T", project=pid)
    draft.put(
        id="tb",
        chunk_kind="table",
        table={"header": ["ID", "Title"], "rows": [["I1", "alpha"], ["I2", "beta"]]},
        caption="Issue register",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="tb")
    out = tmp_path / "tb.docx"
    export_docx(hub.live_store, ref, target_path=out)

    doc = docx.Document(str(out))
    assert len(doc.tables) == 1
    t = doc.tables[0]
    assert len(t.rows) == 3 and len(t.columns) == 2  # header + 2 body rows
    assert [c.text for c in t.rows[0].cells] == ["ID", "Title"]
    assert [c.text for c in t.rows[1].cells] == ["I1", "alpha"]
    assert t.rows[0].cells[0].paragraphs[0].runs[0].bold  # header bold
    # only the header row repeats on each page (gr725249)
    assert [bool(r._tr.xpath("w:trPr/w:tblHeader")) for r in t.rows] == [
        True,
        False,
        False,
    ]
    # caption renders auto-numbered, like a figure's "Figure N:" (gr592852);
    # pipe markdown not dumped
    paras = [p.text for p in doc.paragraphs]
    assert "Table 1: Issue register" in paras
    assert not any("| ID | Title |" in p for p in paras)
    # the title is styled, not directly formatted (gr725239)
    cap = next(p for p in doc.paragraphs if p.text == "Table 1: Issue register")
    assert cap.style.name == "Table Caption"
    assert cap.style.base_style.name == "Caption"
    assert cap.runs[0].style.name == "Caption Label"
    assert all(r.bold is None for r in cap.runs)
    assert cap.style.font.bold is True
    assert cap.style.paragraph_format.keep_with_next is True


def test_table_numbers_in_document_order_and_cross_reference(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """gr592852: tables number in document order like figures (each on its
    own counter), and a ``[dc<id>]`` cross-ref to a table prints "table N"
    — never the enclosing section's number."""
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="tn", title="T", project=pid)
    t1 = draft.put(
        id="tn",
        chunk_kind="table",
        table={"header": ["a"], "rows": [[1]]},
        caption="first legend",
        at={"last": True},
    )
    m1 = re.search(r"dc\d+", t1.body)
    assert m1
    t1_dc = m1.group(0)
    t2 = draft.put(
        id="tn",
        chunk_kind="table",
        table={"header": ["b"], "rows": [[2]]},
        at={"last": True},
    )
    m2 = re.search(r"dc\d+", t2.body)
    assert m2
    t2_dc = m2.group(0)
    draft.put(
        id="tn",
        chunk_kind="paragraph",
        text=f"Compare [{t1_dc}] with [{t2_dc}] here.",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="tn")
    out = tmp_path / "tn.docx"
    export_docx(hub.live_store, ref, target_path=out)

    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    assert "Table 1: first legend" in text
    assert "Table 2" in text  # caption-less table still shows its number
    assert "Compare Table 1 with Table 2 here." in text


def test_render_byline_names_marks_and_ror_link() -> None:
    """The byline block: names with superscript marks (when >1 affiliation)
    + one affiliation paragraph each, ROR org rendered as a real hyperlink.
    Pure over a python-docx Document (no store)."""
    from precis.export.docx import _render_byline
    from precis.utils.authors import build_byline

    doc = docx.Document()
    doc.add_heading("Title", level=0)
    byline = build_byline(
        [
            {"name": "Doe, Jane", "affiliation": "MIT", "ror": "https://ror.org/x"},
            {"name": "Roe, John", "affiliation": "Caltech"},
        ]
    )
    _render_byline(doc, byline)
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "Jane Doe" in text and "John Roe" in text
    # superscript marks present as run text on the names paragraph
    names_p = doc.paragraphs[1]
    assert any(r.font.superscript and r.text in ("1", "2") for r in names_p.runs)
    # the ROR affiliation is a real external hyperlink relationship
    xml = doc.paragraphs[2]._p.xml
    assert "hyperlink" in xml
    rels = doc.part.rels
    assert any(r.reltype.endswith("hyperlink") for r in rels.values())


def test_render_byline_orcid_is_a_superscript_id_link() -> None:
    """An author's ORCID renders as a superscript ``iD`` hyperlink to the
    orcid.org record, right after the name."""
    from precis.export.docx import _render_byline
    from precis.utils.authors import build_byline

    doc = docx.Document()
    doc.add_heading("Title", level=0)
    _render_byline(
        doc,
        build_byline(
            [
                {"name": "Doe, Jane", "orcid": "0000-0002-1825-0097"},
                {"name": "Roe, John"},
            ]
        ),
    )
    names_p = doc.paragraphs[1]
    xml = names_p._p.xml
    assert "hyperlink" in xml and ">iD<" in xml
    assert 'w:vertAlign w:val="superscript"' in xml
    rels = doc.part.rels
    assert any(
        r.reltype.endswith("hyperlink")
        and r.target_ref == "https://orcid.org/0000-0002-1825-0097"
        for r in rels.values()
    )


def test_render_byline_empty_authors_is_noop() -> None:
    from precis.export.docx import _render_byline
    from precis.utils.authors import build_byline

    doc = docx.Document()
    doc.add_heading("Title", level=0)
    before = len(doc.paragraphs)
    _render_byline(doc, build_byline(None))
    assert len(doc.paragraphs) == before


class _RefStore:
    """Minimal store for the pure reference/EndNote resolvers: resolves a
    slug to a paper/patent/datasheet ref, no DOI/arXiv aliases."""

    #: Set by the SI tests to a stub whose ``connection()`` yields a dummy.
    pool: Any = None

    def __init__(self, refs):
        self._refs = refs  # (kind, slug) -> Ref-ish

    def get_ref(self, *, kind, id):
        return self._refs.get((kind, id))

    def identifiers_for_refs(self, ref_ids):
        return {}


def test_format_reference_resolves_datasheet_not_stub() -> None:
    """A cited datasheet resolves to a real reference line — parity with the
    .bib path (gr52396) — not the 'missing source' stub."""
    from types import SimpleNamespace

    from precis.export.docx import _format_reference

    store = _RefStore(
        {
            ("datasheet", "stm32f4"): SimpleNamespace(
                id=7,
                slug="stm32f4",
                kind="datasheet",
                title="STM32F4 Reference Manual",
                authors=[{"name": "STMicroelectronics"}],
                year=2019,
                meta={},
            )
        }
    )
    warnings: list[str] = []
    line = _format_reference(store, "stm32f4", warnings)
    assert "STM32F4 Reference Manual" in line
    assert "STMicroelectronics" in line
    assert "[Datasheet]" in line  # sub-type genre label
    assert "missing source" not in line


def test_format_reference_datasheet_uses_vendor_subtype_part() -> None:
    """A bylineless datasheet: vendor stands in as the org, the sub-type
    label + documented part ride along — parity with the .bib @manual entry."""
    from types import SimpleNamespace

    from precis.export.docx import _format_reference

    store = _RefStore(
        {
            ("datasheet", "esp32c3"): SimpleNamespace(
                id=9,
                slug="esp32c3",
                kind="datasheet",
                title="ESP32-C3 App Note",
                authors=None,
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
    line = _format_reference(store, "esp32c3", warnings)
    assert "Espressif Systems" in line
    assert "(2022)" in line
    assert "[Application note]" in line
    assert "Part C2934569" in line
    assert warnings == []


def test_format_reference_software_record_line() -> None:
    """A typed whole-record cite (gr514384): genre label + publisher +
    version, no venue — the docx counterpart of the .bib ``@software``."""
    from types import SimpleNamespace

    from precis.export.docx import _format_reference

    store = _RefStore(
        {
            ("paper", "precismcp"): SimpleNamespace(
                id=496283,
                slug="precismcp",
                kind="paper",
                title="precis-mcp",
                authors=[{"name": "Stamm, Reto"}],
                year=2026,
                meta={
                    "record_type": "software",
                    "publisher": "Zenodo",
                    "version": "1.2.3",
                },
            )
        }
    )
    line = _format_reference(store, "precismcp", [])
    assert "Stamm, Reto (2026). precis-mcp." in line
    assert "[Software]" in line
    assert "Zenodo" in line
    assert "version 1.2.3" in line


def test_resolve_source_record_bib_type() -> None:
    """``_resolve_source`` maps a typed record's ``bib_type`` through
    ``record_bib_type`` so the EndNote field gets ref-type Dataset."""
    from types import SimpleNamespace

    from precis.export.docx import _resolve_source

    store = _RefStore(
        {
            ("paper", "corpus1"): SimpleNamespace(
                id=2,
                slug="corpus1",
                kind="paper",
                title="A corpus",
                authors=None,
                year=2025,
                meta={"record_type": "dataset", "publisher": "Zenodo"},
            )
        }
    )
    source = _resolve_source(store, "corpus1")
    assert source is not None
    assert source["bib_type"] == "dataset"


# ── Taproot claim-hub finding handle (Phase 1 — living citations reach
# draft export): a [fi<id>] finding handle that resolves to a
# TAPROOT:claim hub cites its *derived* establishes originator(s) via the
# ONE shared resolver (precis.taproot.cite), not a stored
# primary_cite_key. Mirrors tests/test_export_latex.py's hub coverage.


def _mint_hub_claim(store: Store) -> int:
    from precis.taproot.canon import CanonicalClaim
    from precis.taproot.hub import mint_hub

    claim = CanonicalClaim(
        sentence="Pd/C catalyzes Suzuki coupling at room temperature with a mild base.",
        scope={"material": "Pd/C", "method": "Suzuki coupling", "regime": "RT"},
    )
    return mint_hub(store, claim)


def _new_draft_project(hub: Hub) -> int:
    return int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )


def test_hub_finding_single_originator_renders_one_mark(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    from precis.taproot.hub import attach_evidence
    from precis.utils import handle_registry

    hub_ref = _mint_hub_claim(hub.live_store)
    origin = hub.live_store.insert_ref(
        kind="paper", slug="docxo01", title="Original report", year=2001, meta={}
    ).id
    follow = hub.live_store.insert_ref(
        kind="paper", slug="docxf05", title="Follow-up", year=2005, meta={}
    ).id
    attach_evidence(
        hub.live_store, hub_ref_id=hub_ref, paper_ref_id=origin, role="corroborates"
    )
    attach_evidence(
        hub.live_store, hub_ref_id=hub_ref, paper_ref_id=follow, role="corroborates"
    )
    hub.live_store.add_link(src_ref_id=follow, dst_ref_id=origin, relation="cites")
    finding_handle = handle_registry.format_handle("finding", hub_ref)

    pid = _new_draft_project(hub)
    draft.put(id="dhub1", title="T", project=pid)
    draft.put(
        id="dhub1",
        chunk_kind="paragraph",
        text=f"Living citation [{finding_handle}].",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="dhub1")
    out = tmp_path / "dhub1.docx"
    res = export_docx(hub.live_store, ref, target_path=out)

    assert res.cited_slugs == ["docxo01"]  # the derived originator, not corroborator
    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    assert "[1]" in text
    assert "Original report" in text


def test_hub_finding_multiple_originators_renders_two_marks(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """docx has no ``\\cite{a,b}`` literal — a multi-originator hub emits
    one numbered mark PER key, per-key calls into the existing ``_cite``
    path (mirrors the LaTeX exporter's grouped-\\cite counterpart)."""
    from precis.taproot.hub import attach_evidence
    from precis.utils import handle_registry

    hub_ref = _mint_hub_claim(hub.live_store)
    a = hub.live_store.insert_ref(
        kind="paper", slug="docxa01", title="A — first report", year=2001, meta={}
    ).id
    b = hub.live_store.insert_ref(
        kind="paper", slug="docxb02", title="B — second report", year=2002, meta={}
    ).id
    citer = hub.live_store.insert_ref(
        kind="paper", slug="docxc09", title="Citer", year=2009, meta={}
    ).id
    for p in (a, b, citer):
        attach_evidence(
            hub.live_store, hub_ref_id=hub_ref, paper_ref_id=p, role="corroborates"
        )
    hub.live_store.add_link(src_ref_id=citer, dst_ref_id=a, relation="cites")
    hub.live_store.add_link(src_ref_id=citer, dst_ref_id=b, relation="cites")
    finding_handle = handle_registry.format_handle("finding", hub_ref)

    pid = _new_draft_project(hub)
    draft.put(id="dhub2", title="T", project=pid)
    draft.put(
        id="dhub2",
        chunk_kind="paragraph",
        text=f"Living citation [{finding_handle}].",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="dhub2")
    out = tmp_path / "dhub2.docx"
    res = export_docx(hub.live_store, ref, target_path=out)

    assert res.cited_slugs == ["docxa01", "docxb02"]
    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    assert "[1]" in text and "[2]" in text
    assert "A — first report" in text and "B — second report" in text


def test_hub_finding_multi_cite_gets_doi_ul_hyperlink_runs(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """Cite-links: each numbered mark gets a small doi/UL hyperlink-run
    pair right after it (mirrors the LaTeX exporter's ``\\href`` pair),
    and none at all with both ``doi_links=False`` and
    ``library_links=False``."""
    from precis.taproot.hub import attach_evidence
    from precis.utils import handle_registry

    hub_ref = _mint_hub_claim(hub.live_store)
    a = hub.live_store.insert_ref(
        kind="paper", slug="docxa21", title="A — first report", year=2001, meta={}
    ).id
    b = hub.live_store.insert_ref(
        kind="paper", slug="docxb22", title="B — second report", year=2002, meta={}
    ).id
    citer = hub.live_store.insert_ref(
        kind="paper", slug="docxc29", title="Citer", year=2009, meta={}
    ).id
    for p in (a, b, citer):
        attach_evidence(
            hub.live_store, hub_ref_id=hub_ref, paper_ref_id=p, role="corroborates"
        )
    hub.live_store.add_link(src_ref_id=citer, dst_ref_id=a, relation="cites")
    hub.live_store.add_link(src_ref_id=citer, dst_ref_id=b, relation="cites")
    hub.live_store.insert_ref_identifiers(a, [("doi", "10.1/docxa21", "manual")])
    hub.live_store.insert_ref_identifiers(b, [("doi", "10.1/docxb22", "manual")])
    finding_handle = handle_registry.format_handle("finding", hub_ref)

    pid = _new_draft_project(hub)
    draft.put(id="dhub4", title="T", project=pid)
    draft.put(
        id="dhub4",
        chunk_kind="paragraph",
        text=f"Living citation [{finding_handle}].",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="dhub4")
    out = tmp_path / "dhub4.docx"
    res = export_docx(hub.live_store, ref, target_path=out)
    assert res.cited_slugs == ["docxa21", "docxb22"]

    hrefs = {
        r.target_ref
        for r in docx.Document(str(out)).part.rels.values()
        if r.reltype.endswith("hyperlink")
    }
    assert "https://doi.org/10.1/docxa21" in hrefs
    assert "https://doi.org/10.1/docxb22" in hrefs
    assert sum("uol.primo.exlibrisgroup.com" in h for h in hrefs) == 2

    out_off = tmp_path / "dhub4-off.docx"
    export_docx(
        hub.live_store,
        ref,
        target_path=out_off,
        doi_links=False,
        library_links=False,
    )
    rels_off = docx.Document(str(out_off)).part.rels
    assert not any(r.reltype.endswith("hyperlink") for r in rels_off.values())


def test_doi_and_library_link_switches_are_independent(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """``doi_links`` and ``library_links`` are independent: doi-on/
    library-off emits only the doi hyperlink; doi-off/library-on emits
    only the library-search hyperlink."""
    a = hub.live_store.insert_ref(
        kind="paper", slug="docxindep1", title="A", year=2001, meta={}
    ).id
    hub.live_store.insert_ref_identifiers(a, [("doi", "10.1/docxindep1", "manual")])

    pid = _new_draft_project(hub)
    draft.put(id="dindep", title="T", project=pid)
    draft.put(
        id="dindep",
        chunk_kind="paragraph",
        text="See [§docxindep1].",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="dindep")

    out_doi_only = tmp_path / "dindep-doi.docx"
    export_docx(
        hub.live_store,
        ref,
        target_path=out_doi_only,
        doi_links=True,
        library_links=False,
    )
    hrefs_doi_only = {
        r.target_ref
        for r in docx.Document(str(out_doi_only)).part.rels.values()
        if r.reltype.endswith("hyperlink")
    }
    assert "https://doi.org/10.1/docxindep1" in hrefs_doi_only
    assert not any("uol.primo.exlibrisgroup.com" in h for h in hrefs_doi_only)

    out_lib_only = tmp_path / "dindep-lib.docx"
    export_docx(
        hub.live_store,
        ref,
        target_path=out_lib_only,
        doi_links=False,
        library_links=True,
    )
    hrefs_lib_only = {
        r.target_ref
        for r in docx.Document(str(out_lib_only)).part.rels.values()
        if r.reltype.endswith("hyperlink")
    }
    assert "https://doi.org/10.1/docxindep1" not in hrefs_lib_only
    assert any("uol.primo.exlibrisgroup.com" in h for h in hrefs_lib_only)


def test_hub_finding_doi_hyperlink_percent_encodes_reserved_chars(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """A legacy DOI with reserved URL chars (``<`` / ``>`` / ``;`` here)
    must be percent-encoded in the hyperlink relationship target — a raw
    ``<`` is not well-formed URL syntax."""
    from precis.taproot.hub import attach_evidence
    from precis.utils import handle_registry

    hub_ref = _mint_hub_claim(hub.live_store)
    a = hub.live_store.insert_ref(
        kind="paper", slug="docxa41", title="A — legacy DOI", year=1998, meta={}
    ).id
    citer = hub.live_store.insert_ref(
        kind="paper", slug="docxc49", title="Citer", year=2009, meta={}
    ).id
    for p in (a, citer):
        attach_evidence(
            hub.live_store, hub_ref_id=hub_ref, paper_ref_id=p, role="corroborates"
        )
    hub.live_store.add_link(src_ref_id=citer, dst_ref_id=a, relation="cites")
    hub.live_store.insert_ref_identifiers(
        a,
        [
            (
                "doi",
                "10.1002/(SICI)1097-0258(19980815/30)17:15/16<1661::AID-SIM968>3.0.CO;2-2",
                "manual",
            )
        ],
    )
    finding_handle = handle_registry.format_handle("finding", hub_ref)

    pid = _new_draft_project(hub)
    draft.put(id="dhub5", title="T", project=pid)
    draft.put(
        id="dhub5",
        chunk_kind="paragraph",
        text=f"Living citation [{finding_handle}].",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="dhub5")
    out = tmp_path / "dhub5.docx"
    res = export_docx(hub.live_store, ref, target_path=out)
    assert res.cited_slugs == ["docxa41"]

    hrefs = {
        r.target_ref
        for r in docx.Document(str(out)).part.rels.values()
        if r.reltype.endswith("hyperlink")
    }
    assert any("%3C1661" in h for h in hrefs)
    assert not any("<" in h or ">" in h or ";" in h for h in hrefs)


def test_hub_finding_no_evidence_renders_no_cite(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    from precis.utils import handle_registry

    hub_ref = _mint_hub_claim(hub.live_store)
    finding_handle = handle_registry.format_handle("finding", hub_ref)

    pid = _new_draft_project(hub)
    draft.put(id="dhub3", title="T", project=pid)
    draft.put(
        id="dhub3",
        chunk_kind="paragraph",
        text=f"Pending [{finding_handle}] evidence.",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="dhub3")
    out = tmp_path / "dhub3.docx"
    res = export_docx(hub.live_store, ref, target_path=out)

    assert res.cited_slugs == []
    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    assert "[1]" not in text
    assert "References" not in text


# ── Taproot Phase 2 — authorial pins reach draft export (docx) ─────────
# Mirrors the LaTeX exporter's pin coverage (tests/test_export_latex.py) —
# the SAME shared `precis.taproot.cite.apply_pin` policy applied through
# the `mentions` grammar's optional `pin` capture group.


def test_pin_replace_renders_pinned_not_derived_originator(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    from precis.taproot.hub import attach_evidence
    from precis.utils import handle_registry

    hub_ref = _mint_hub_claim(hub.live_store)
    origin = hub.live_store.insert_ref(
        kind="paper", slug="docxp01", title="Original report", year=2001, meta={}
    ).id
    follow = hub.live_store.insert_ref(
        kind="paper", slug="docxq01", title="Follow-up", year=2005, meta={}
    ).id
    attach_evidence(
        hub.live_store, hub_ref_id=hub_ref, paper_ref_id=origin, role="corroborates"
    )
    attach_evidence(
        hub.live_store, hub_ref_id=hub_ref, paper_ref_id=follow, role="corroborates"
    )
    hub.live_store.add_link(src_ref_id=follow, dst_ref_id=origin, relation="cites")
    pinned = hub.live_store.insert_ref(
        kind="paper", slug="docxr01", title="Author's pick", meta={}
    ).id
    finding_handle = handle_registry.format_handle("finding", hub_ref)
    pin_handle = handle_registry.format_handle("paper", pinned)

    pid = _new_draft_project(hub)
    draft.put(id="dpin1", title="T", project=pid)
    draft.put(
        id="dpin1",
        chunk_kind="paragraph",
        text=f"Living citation [{finding_handle}>{pin_handle}].",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="dpin1")
    out = tmp_path / "dpin1.docx"
    res = export_docx(hub.live_store, ref, target_path=out)

    assert res.cited_slugs == ["docxr01"]  # pinned handle, not the derived originator
    assert any("reconsider" in w for w in res.warnings)
    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    assert "Author's pick" in text
    assert "Original report" not in text


def test_pin_supplement_renders_derived_plus_pinned(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    from precis.taproot.hub import attach_evidence
    from precis.utils import handle_registry

    hub_ref = _mint_hub_claim(hub.live_store)
    origin = hub.live_store.insert_ref(
        kind="paper", slug="docxp02", title="Original report", year=2001, meta={}
    ).id
    follow = hub.live_store.insert_ref(
        kind="paper", slug="docxq02", title="Follow-up", year=2005, meta={}
    ).id
    attach_evidence(
        hub.live_store, hub_ref_id=hub_ref, paper_ref_id=origin, role="corroborates"
    )
    attach_evidence(
        hub.live_store, hub_ref_id=hub_ref, paper_ref_id=follow, role="corroborates"
    )
    hub.live_store.add_link(src_ref_id=follow, dst_ref_id=origin, relation="cites")
    pinned = hub.live_store.insert_ref(
        kind="paper", slug="docxr02", title="Extra evidence", meta={}
    ).id
    finding_handle = handle_registry.format_handle("finding", hub_ref)
    pin_handle = handle_registry.format_handle("paper", pinned)

    pid = _new_draft_project(hub)
    draft.put(id="dpin2", title="T", project=pid)
    draft.put(
        id="dpin2",
        chunk_kind="paragraph",
        text=f"Living citation [{finding_handle}+{pin_handle}].",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="dpin2")
    out = tmp_path / "dpin2.docx"
    res = export_docx(hub.live_store, ref, target_path=out)

    assert res.cited_slugs == ["docxp02", "docxr02"]  # derived + pinned, both present
    assert not any("reconsider" in w for w in res.warnings)
    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    assert "[1]" in text and "[2]" in text


def test_pin_on_non_hub_finding_ignored_with_warning(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    from precis.handlers.finding import FindingHandler

    _seed_paper(hub.live_store, "docxn01", "Established plain finding", 2019)
    resp = FindingHandler(hub=hub).put(
        title="t", body="b", scope={}, cited_in="docxn01"
    )
    ref_id = int(resp.body.split("id=")[1].split()[0].rstrip(",.()"))
    hub.live_store.update_ref(ref_id, meta_patch={"primary_cite_key": "docxn01"})
    from precis.utils import handle_registry

    finding_handle = handle_registry.format_handle("finding", ref_id)

    pid = _new_draft_project(hub)
    draft.put(id="dpin3", title="T", project=pid)
    draft.put(
        id="dpin3",
        chunk_kind="paragraph",
        text=f"Established [{finding_handle}>pa5].",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="dpin3")
    out = tmp_path / "dpin3.docx"
    res = export_docx(hub.live_store, ref, target_path=out)

    assert res.cited_slugs == ["docxn01"]  # unchanged — pin is meaningless here
    assert any("pin on" in w and "ignored" in w for w in res.warnings)


def _si_docx_ctx(monkeypatch, *, with_parent: bool):
    import contextlib
    from types import SimpleNamespace

    from precis.export import docx as dx
    from precis.store import si_links

    parent = SimpleNamespace(id=1, slug="parent24", kind="paper", pdf_role=None)
    si = SimpleNamespace(id=2, slug="parent24si", kind="paper", pdf_role="supplement")
    store = _RefStore({("paper", "parent24"): parent, ("paper", "parent24si"): si})
    store.pool = type(
        "P", (), {"connection": lambda s: contextlib.nullcontext(object())}
    )()
    monkeypatch.setattr(
        si_links,
        "supplement_parent",
        lambda _conn, rid: (1, "parent24") if with_parent and rid == 2 else None,
    )
    return dx._Ctx(
        store=store,
        known_handles=set(),
        doi_links=False,
        library_links=False,
        library_label="x",
        library_search_url="x",
    )


def test_si_cite_numbers_as_parent_with_si_note(monkeypatch) -> None:
    import docx

    from precis.export import docx as dx

    ctx = _si_docx_ctx(monkeypatch, with_parent=True)
    para = docx.Document().add_paragraph()
    dx._cite("parent24si", ctx, para)
    dx._flush_cites(ctx, para)
    assert ctx.cited == ["parent24"]
    assert para.text == "[1] (SI)"


def test_si_cite_without_parent_falls_back_with_warning_docx(monkeypatch) -> None:
    import docx

    from precis.export import docx as dx

    ctx = _si_docx_ctx(monkeypatch, with_parent=False)
    para = docx.Document().add_paragraph()
    dx._cite("parent24si", ctx, para)
    dx._flush_cites(ctx, para)
    assert ctx.cited == ["parent24si"]
    assert para.text == "[1]"
    assert any("no live parent" in w for w in ctx.warnings)


def test_si_mark_survives_after_plain_mark_of_same_parent(monkeypatch) -> None:
    import docx

    from precis.export import docx as dx

    ctx = _si_docx_ctx(monkeypatch, with_parent=True)
    para = docx.Document().add_paragraph()
    dx._cite("parent24", ctx, para)
    dx._cite("parent24si", ctx, para)  # same parent, SI flag differs
    dx._cite("parent24si", ctx, para)  # identical consecutive -> collapses
    dx._flush_cites(ctx, para)
    assert para.text == "[1][1] (SI)"


def test_si_mark_separates_a_directly_adjacent_cite(monkeypatch) -> None:
    """A cite mark right after ``[n] (SI)`` takes a separating space —
    ``(SI)[4`` read as one blob (gr491600's nit)."""
    import docx

    from precis.export import docx as dx

    ctx = _si_docx_ctx(monkeypatch, with_parent=True)
    para = docx.Document().add_paragraph()
    dx._cite("parent24si", ctx, para)
    dx._cite("parent24", ctx, para)  # the same paper — a second [1] mark
    dx._flush_cites(ctx, para)
    assert para.text == "[1] (SI) [1]"
    # intervening prose, not an adjacent mark: no added space
    ctx2 = _si_docx_ctx(monkeypatch, with_parent=True)
    para2 = docx.Document().add_paragraph()
    dx._cite("parent24si", ctx2, para2)
    dx._flush_cites(ctx2, para2)
    para2.add_run(" then ")
    dx._cite("parent24", ctx2, para2)
    dx._flush_cites(ctx2, para2)
    assert para2.text == "[1] (SI) then [1]"


def test_mathrm_renders_upright_omml() -> None:
    """``\\mathrm{C}_{60}`` (the repaired ``C$_{60}$``) must come out as
    upright (``m:sty p``) OMML runs, single- and multi-character alike."""
    pytest.importorskip("latex2mathml")

    from precis.export.omml import latex_to_omml

    ns = {"m": "http://schemas.openxmlformats.org/officeDocument/2006/math"}
    for src, base in ((r"\mathrm{C}_{60}", "C"), (r"\mathrm{WS}_2", "WS")):
        omath = latex_to_omml(src)
        assert omath is not None
        runs = omath.xpath("//m:e/m:r", namespaces=ns)
        assert "".join(r.xpath("string(m:t)", namespaces=ns) for r in runs) == base
        assert all(r.xpath("m:rPr/m:sty/@m:val", namespaces=ns) == ["p"] for r in runs)


# ── fidelity vs. the LaTeX exporter (docx-fidelity pass) ───────────────


class _NonHubConn:
    def execute(self, *_a, **_k):
        return self

    def fetchone(self):
        return None


class _NonHubPool:
    def connection(self):
        import contextlib

        return contextlib.nullcontext(_NonHubConn())


class _FindingFake:
    """Store for a plain finding handle with no cite key of its own."""

    def __init__(self, refs):
        self._refs = refs
        self.pool = _NonHubPool()

    def fetch_refs_by_ids(self, ids):
        from types import SimpleNamespace

        return {i: self._refs.get(i, SimpleNamespace(meta={})) for i in ids}

    def tags_for(self, _ref_id):
        return []

    def get_ref(self, **_k):
        return None


def _render_text(text: str, ctx) -> str:
    from precis.export import docx as dx

    para = docx.Document().add_paragraph()
    dx._render_inline(text, ctx, para)
    return para.text


def test_conjunction_hub_cites_union_of_conjunct_sources(monkeypatch) -> None:
    """A finding with no cite key of its own (conjunction hub) cites its
    atoms' sources, like LaTeX — it used to vanish from docx."""
    from precis.export import docx as dx

    monkeypatch.setattr(dx, "_conjunct_cite_keys", lambda _s, _pk: ["a21", "b22"])
    monkeypatch.setattr(dx, "_render_trust_mark", lambda *_a: None)
    ctx = dx._Ctx(
        store=_FindingFake({}),
        known_handles=set(),
        doi_links=False,
        library_links=False,
        library_label="x",
        library_search_url="x",
    )
    out = _render_text("claim [fi7].", ctx)
    assert out == "claim [1, 2]."  # adjacent cites merge, like the PDF
    assert ctx.cited == ["a21", "b22"]


def test_sourceless_finding_cite_pulls_space_before_punctuation(monkeypatch) -> None:
    from precis.export import docx as dx

    monkeypatch.setattr(dx, "_conjunct_cite_keys", lambda _s, _pk: [])
    monkeypatch.setattr(dx, "_render_trust_mark", lambda *_a: None)
    ctx = dx._Ctx(
        store=_FindingFake({}),
        known_handles=set(),
        doi_links=False,
        library_links=False,
        library_label="x",
        library_search_url="x",
    )
    out = _render_text("in frameworks [fi7]. And (~130 GPa [fi7]) end", ctx)
    assert out == "in frameworks. And (~130 GPa) end"
    assert any("no citable source" in w for w in ctx.warnings)


def test_cite_run_does_not_collapse_across_paragraphs(monkeypatch) -> None:
    """A paragraph that opens with the paper the previous one closed on keeps
    its mark: consecutive-cite collapse is scoped to one chunk."""
    from precis.export import docx as dx

    ctx = dx._Ctx(
        store=None,
        known_handles=set(),
        doi_links=False,
        library_links=False,
        library_label="x",
        library_search_url="x",
    )
    assert _render_text("one [§smith20].", ctx) == "one [1]."
    assert _render_text("[§smith20] two", ctx) == "[1] two"


def test_si_cite_via_alias_shares_the_parents_number(monkeypatch) -> None:
    """The same paper cited plainly (under an alias key) and via its SI
    record must get ONE number; SI keeps the '(SI)' postnote."""
    from types import SimpleNamespace

    import docx as _docx

    from precis.export import docx as dx

    ctx = _si_docx_ctx(monkeypatch, with_parent=True)
    # an alias key resolves to the same ref whose canonical slug is parent24
    ctx.store._refs[("paper", "parent24alias")] = SimpleNamespace(
        id=1, slug="parent24", kind="paper", pdf_role=None
    )
    para = _docx.Document().add_paragraph()
    dx._cite("parent24alias", ctx, para)
    dx._flush_cites(ctx, para)
    para.add_run(" and ")
    dx._cite("parent24si", ctx, para)
    dx._flush_cites(ctx, para)
    assert ctx.cited == ["parent24"]
    assert para.text == "[1] and [1] (SI)"


def test_xref_numbers_follow_latex_section_counting() -> None:
    """The seeded title is skipped and the nested body lifted a level
    (Intro is 1, a depth-2 child 1.1); the draft's own Glossary heading is
    skipped; figures count in reading order; run-in headings (depth >= 3)
    resolve to their enclosing section."""
    from types import SimpleNamespace as NS

    from precis.export.docx import _xref_numbers

    def ch(dc, kind, depth, text=""):
        return NS(dc=dc, chunk_kind=kind, depth=depth, text=text)

    chunks = [
        ch("dc1", "heading", 0, "Title"),
        ch("dc2", "heading", 1, "Intro"),
        ch("dc3", "figure", 2),
        ch("dc4", "heading", 1, "Applications"),
        ch("dc5", "heading", 2, "Catalysis"),
        ch("dc6", "heading", 2, "Batteries"),
        ch("dc7", "heading", 3, "Run-in"),
        ch("dc8", "figure", 2),
        ch("dc9", "heading", 0, "Glossary"),
        ch("dc10", "term", 1, "x"),
        ch("dc11", "heading", 4, "Run-in"),
    ]
    xref, shown = _xref_numbers(chunks, "  title ")
    assert "dc1" not in xref  # the seeded title heading is not a section
    assert xref["dc2"] == ("Section", "1")
    assert xref["dc4"] == ("Section", "2")
    assert xref["dc6"] == ("Section", "2.2")
    assert xref["dc7"] == ("Section", "2.2.1")
    # sections beside the title at depth 0 (no lift): nothing is shifted
    flat = [ch("dc1", "heading", 0, "Title"), ch("dc2", "heading", 0, "Intro")]
    assert _xref_numbers(flat, "Title")[0]["dc2"] == ("Section", "1")
    # an untitled lookup keeps the old behaviour: depth-0 heading counts
    assert _xref_numbers(chunks)[0]["dc1"] == ("Section", "1")
    assert xref["dc3"] == ("Figure", "1") and xref["dc8"] == ("Figure", "2")
    assert "dc9" not in xref and "dc11" not in shown and "dc7" in shown


def test_xref_numbers_resolve_paragraphs_and_back_matter_like_latex() -> None:
    """A cross-ref to a plain paragraph prints its enclosing section (what
    ``\\cref`` does for a label inside a section); journal back matter is
    unnumbered and a cross-ref to it prints its name (``\\nameref``)."""
    from types import SimpleNamespace as NS

    from precis.export.docx import _xref_numbers

    def ch(dc, kind, depth, text=""):
        return NS(dc=dc, chunk_kind=kind, depth=depth, text=text)

    chunks = [
        ch("abs", "paragraph", 0, "The abstract."),
        ch("h1", "heading", 0, "Introduction"),
        ch("p1", "paragraph", 1, "body"),
        ch("h11", "heading", 1, "Scope"),
        ch("p2", "paragraph", 2, "body"),
        ch("h2", "heading", 0, "Results"),
        ch("ul", "ulist", 1),
        ch("it", "item", 2, "an item"),
        ch("bm", "heading", 0, "Author Information"),
        ch("bm1", "heading", 1, "Notes"),
        ch("p3", "paragraph", 1, "thanks"),
    ]
    xref, shown = _xref_numbers(chunks, "Paper")
    assert "abs" not in xref  # front matter: nothing to print
    assert xref["p1"] == ("Section", "1")
    assert xref["p2"] == ("Section", "1.1")
    assert xref["it"] == ("Section", "2") and "ul" not in xref
    assert xref["bm"] == ("", "Author Information")
    assert xref["bm1"] == ("", "Notes")
    assert "bm" not in shown and "bm1" not in shown
    assert xref["p3"] == ("Section", "2")  # \section* does not step the counter


def test_docx_without_seeded_title_chunk_prints_ref_title_first(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """A draft whose reading order opens with the abstract (the seeded
    title heading retired) still gets the title block up front, and its
    first depth-0 section is numbered "1", not styled as the title."""
    pid = _new_draft_project(hub)
    draft.put(id="dnt", title="Nanobuds", project=pid)
    ref = hub.live_store.get_ref(kind="draft", id="dnt")
    assert ref is not None
    title_h = hub.live_store.drafts.reading_order(ref.id)[0].handle
    draft.put(
        id="dnt", chunk_kind="paragraph", text="The abstract.", at={"first": True}
    )
    draft.put(id="dnt", chunk_kind="heading", text="Introduction", at={"last": True})
    draft.put(id="dnt", chunk_kind="paragraph", text="Body.", at={"last": True})
    draft.delete(id=f"¶{title_h}")
    out = tmp_path / "dnt.docx"
    export_docx(hub.live_store, ref, target_path=out)
    paras = docx.Document(str(out)).paragraphs
    texts = [p.text for p in paras]
    assert texts[0] == "Nanobuds" and paras[0].style.name == "Title"
    assert "The abstract." in texts  # no synthetic "Abstract" heading
    intro = next(p for p in paras if p.text.endswith("Introduction"))
    assert intro.text == "1 Introduction" and intro.style.name == "Heading 1"


def test_bare_dc_crossref_prints_section_and_figure_numbers() -> None:
    from precis.export import docx as dx

    ctx = dx._Ctx(
        store=None,
        known_handles=set(),
        xref={"dc4": ("Section", "1.5.5"), "dc3": ("Figure", "1")},
        dc_handles={"dc3", "dc4", "dc5"},
        legacy_to_dc={"Abc": "dc4"},
    )
    assert (
        _render_text("(see [dc4]) and [¶dc4]", ctx)
        == "(see Section 1.5.5) and Section 1.5.5"
    )
    assert _render_text("Shown in [dc3].", ctx) == "Shown in Figure 1."
    # sentence start capitalises
    assert (
        _render_text("[dc4] covers it. [dc3] too", ctx)
        == "Section 1.5.5 covers it. Figure 1 too"
    )
    assert _render_text("via [¶Abc]", ctx) == "via Section 1.5.5"
    # authored surface wins; a non-live chunk downgrades with a warning
    # and a neutral placeholder — never the bare handle (gr493181)
    assert _render_text("[the intro](dc4)", ctx) == "the intro"
    assert _render_text("see [dc99]", ctx) == "see (removed)"
    assert any("dc99" in w for w in ctx.warnings)


def test_crossref_words_are_capitalised_everywhere_equations_keep_eq() -> None:
    """gr725244: a reference to a figure, table or section prints the full
    capitalised word mid-sentence too; an equation keeps ``Eq. (N)`` and
    reads ``Equation (N)`` only at a sentence start."""
    from precis.export import docx as dx

    ctx = dx._Ctx(
        store=None,
        known_handles=set(),
        xref={
            "dc1": ("Figure", "2"),
            "dc2": ("Table", "1"),
            "dc3": ("Section", "5.4"),
        },
        eq_numbers={"dc4": 1},
        dc_handles={"dc1", "dc2", "dc3", "dc4"},
    )
    assert (
        _render_text("in [dc1], [dc2] and [dc3]", ctx)
        == "in Figure 2, Table 1 and Section 5.4"
    )
    assert _render_text("by [dc4]", ctx) == "by Eq. (1)"
    assert _render_text("Done. [dc4] holds", ctx) == "Done. Equation (1) holds"


def test_retired_xref_target_prints_placeholder_or_replacement() -> None:
    """A cross-ref to a retired chunk prints "a removed <noun>" (never the
    bare ``dc`` handle) and resolves to its recorded live replacement when
    one exists (gr493181 — the nanobuds paragraph printed ``dc4300876`` /
    ``dc4300877`` as literals)."""
    from types import SimpleNamespace

    from precis.export import docx as dx

    retired_fig = SimpleNamespace(
        chunk_id=99,
        ref_id=1,
        chunk_kind="figure",
        retired=True,
        meta={},
    )
    retired_sup = SimpleNamespace(
        chunk_id=98,
        ref_id=1,
        chunk_kind="figure",
        retired=True,
        meta={"superseded_by": "dc5"},
    )
    retired_link = SimpleNamespace(
        chunk_id=97,
        ref_id=1,
        chunk_kind="figure",
        retired=True,
        meta={},
    )
    store = SimpleNamespace(
        drafts=SimpleNamespace(
            get_draft_chunk=lambda h: {
                "dc99": retired_fig,
                "dc98": retired_sup,
                "dc97": retired_link,
            }.get(h)
        ),
        fetch_refs_by_ids=lambda ids: {},
        # a chunk-level "supersedes" edge: dc6 -[supersedes]-> dc97
        links_for=lambda _rid, **kw: (
            [SimpleNamespace(dst_chunk_id=97, src_chunk_id=6)]
            if kw.get("relation") == "supersedes"
            else []
        ),
    )
    ctx = dx._Ctx(
        store=store,
        known_handles=set(),
        xref={"dc5": ("Figure", "2"), "dc6": ("Figure", "3")},
        dc_handles={"dc5", "dc6"},
        doi_links=False,
        library_links=False,
        library_label="x",
        library_search_url="x",
    )
    assert _render_text("models of [dc99] here", ctx) == (
        "models of a removed figure here"
    )
    # meta.superseded_by → the live replacement's number
    assert _render_text("models of [dc98] here", ctx) == "models of Figure 2 here"
    # a chunk-level supersedes link resolves the same way
    assert _render_text("models of [dc97] here", ctx) == "models of Figure 3 here"
    assert any("dc99" in w for w in ctx.warnings)
    assert any("resolved to its replacement dc5" in w for w in ctx.warnings)
    assert any("resolved to its replacement dc6" in w for w in ctx.warnings)


def test_retired_xref_end_to_end_on_a_fixture_draft(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """Fixture draft: retire the figure a paragraph cross-references —
    the docx prints the placeholder, not the dead handle (gr493181)."""
    pid = _new_draft_project(hub)
    draft.put(id="dret", title="T", project=pid)
    ref = hub.live_store.get_ref(kind="draft", id="dret")
    assert ref is not None
    title_h = hub.live_store.drafts.reading_order(ref.id)[0].handle
    fig = hub.live_store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="figure",
        text="Fig 1. A widget.",
        at={"after": title_h},
        split=False,
    )[0]
    draft.put(
        id="dret",
        chunk_kind="paragraph",
        text=f"Models of [{fig.dc}] follow.",
        at={"last": True},
    )
    hub.live_store.drafts.retire_chunk(fig.handle)
    out = tmp_path / "dret.docx"
    res = export_docx(hub.live_store, ref, target_path=out)
    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    assert fig.dc not in text
    assert "Models of a removed figure follow." in text
    assert any(fig.dc in w for w in res.warnings)


def test_export_numbers_headings_and_resolves_crossrefs(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    import re

    pid = _new_draft_project(hub)
    draft.put(id="dxr", title="T", project=pid)
    h1 = draft.put(id="dxr", chunk_kind="heading", text="Intro", at={"last": True})
    h1_match = re.search(r"dc\d+", h1.body)
    assert h1_match is not None
    h1_dc = h1_match.group(0)
    draft.put(id="dxr", chunk_kind="heading", text="Methods", at={"last": True})
    draft.put(
        id="dxr",
        chunk_kind="paragraph",
        text=f"As argued in [{h1_dc}], things hold (see [¶{h1_dc}]).",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="dxr")
    out = tmp_path / "dxr.docx"
    export_docx(hub.live_store, ref, target_path=out)
    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    assert "As argued in Section" in text
    assert "(see Section" in text
    assert "Intro" in text


def test_aligned_equation_converts_one_omml_line_per_row() -> None:
    from precis.export.docx import _render_equation

    doc = docx.Document()
    body = (
        r" \begin{aligned} \sum_{n=4}^{8} (6 - n)\,P_n &= 6\chi, \\ "
        r"2P_4 + P_5 - P_7 - 2P_8 &= 6\chi. \end{aligned} "
    )
    _render_equation(doc, body, 2, False)
    paras = doc.paragraphs
    assert len(paras) == 2
    xml = "".join(p._p.xml for p in paras)
    assert "begin{aligned}" not in xml and "&amp;" not in xml
    assert xml.count("<m:oMath") == 2  # both rows went through OMML
    assert "(2)" not in paras[0].text and paras[1].text.endswith("(2)")
    # unnumbered (starred) variant: rows, no label
    doc2 = docx.Document()
    _render_equation(doc2, body, None, True)
    assert len(doc2.paragraphs) == 2
    assert not any(" + (" in p.text for p in doc2.paragraphs)


def test_single_line_equation_unchanged_by_aligned_support() -> None:
    from precis.export.docx import _render_equation

    doc = docx.Document()
    _render_equation(doc, r" \chi = V - E + F. ", 1, False)
    assert len(doc.paragraphs) == 1 and doc.paragraphs[0].text.endswith("(1)")


def _abbrev_ctx(abbrevs):
    from precis.export import docx as dx

    return dx._Ctx(store=None, known_handles=set(), abbrevs=abbrevs)


def test_abbrev_plural_first_use_pluralises_the_long_form() -> None:
    ctx = _abbrev_ctx({"CNT": "carbon nanotube"})
    out = _render_text("We study CNTs. Later CNTs again.", ctx)
    assert out == "We study carbon nanotubes (CNTs). Later CNTs again."


def test_abbrev_spelled_out_in_prose_collapses_to_one_expansion() -> None:
    ctx = _abbrev_ctx(
        {
            "HA": "hexylamine",
            "pG": "pristine graphene",
            "CNT": "carbon nanotube",
        }
    )
    out = _render_text(
        "Using hexylamine (HA) and Pristine Graphene (pG) on carbon nanotubes (CNTs);"
        " HA again.",
        ctx,
    )
    assert out == (
        "Using hexylamine (HA) and pristine graphene (pG) on carbon nanotubes"
        " (CNTs); HA again."
    )
    assert "hexylamine (hexylamine" not in out


def test_abbrev_first_use_at_sentence_start_keeps_capital() -> None:
    ctx = _abbrev_ctx({"GGA": "generalized gradient approximation"})
    assert (
        _render_text("GGA works. The GGA fails.", ctx)
        == "Generalized gradient approximation (GGA) works. The GGA fails."
    )
    ctx2 = _abbrev_ctx({"GGA": "generalized gradient approximation"})
    assert (
        _render_text("It is cheap. GGA works.", ctx2)
        == "It is cheap. Generalized gradient approximation (GGA) works."
    )


def test_abbrev_followed_by_subscripted_index_keeps_the_subscript() -> None:
    """``NICS(1)$_{zz}$``: the shared empty-base repair pulled ``1)`` into the
    math, splitting the parenthesis; a pure-script span is a sub run now."""
    from precis.export import docx as dx

    ctx = _abbrev_ctx({"NICS": "nucleus-independent chemical shift"})
    para = docx.Document().add_paragraph()
    dx._render_inline("a NICS(1)$_{zz}$ index and H$_2$O and x$^{2+}$", ctx, para)
    assert para.text == (
        "a nucleus-independent chemical shift (NICS)(1)zz index and H2O and x2+"
    )
    subs = [r.text for r in para.runs if r.font.subscript]
    sups = [r.text for r in para.runs if r.font.superscript]
    assert subs == ["zz", "2"] and sups == ["2+"]
    assert not para._p.xpath(".//m:oMath")


def test_figure_captions_carry_figure_n_label(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """Captions open with "Figure N:" in reading order, and a bare
    [dc<id>] cross-ref to the second figure prints the same number. The
    caption's look comes from Word styles alone (gr725239): the paragraph is
    ``Caption``, the label run ``Caption Label``, and no run carries direct
    bold or italic, so editing the style in Word restyles every caption."""
    pid = _new_draft_project(hub)
    draft.put(id="dfig", title="T", project=pid)
    ref = hub.live_store.get_ref(kind="draft", id="dfig")
    assert ref is not None
    title_h = hub.live_store.drafts.reading_order(ref.id)[0].handle
    f1 = hub.live_store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="figure",
        text="First widget.",
        at={"after": title_h},
        split=False,
    )[0]
    f2 = hub.live_store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="figure",
        text="Second widget.",
        at={"after": f1.handle},
        split=False,
    )[0]
    dc2 = hub.live_store.drafts.reading_order(ref.id)[2].dc
    assert f2.chunk_id
    draft.put(
        id="dfig",
        chunk_kind="paragraph",
        text=f"As in [{dc2}].",
        at={"last": True},
    )
    out = tmp_path / "dfig.docx"
    export_docx(hub.live_store, ref, target_path=out)
    doc = docx.Document(str(out))
    paras = doc.paragraphs
    texts = [p.text for p in paras]
    assert "Figure 1: First widget." in texts
    assert "Figure 2: Second widget." in texts
    assert "As in Figure 2." in texts
    cap = next(p for p in paras if p.text.startswith("Figure 1:"))
    assert cap.style.name == "Caption"
    assert cap.alignment is None
    assert cap.runs[0].style.name == "Caption Label"
    assert all(r.bold is None and r.italic is None for r in cap.runs)
    caption_style = doc.styles["Caption"]
    assert (
        caption_style.paragraph_format.alignment
        == docx.enum.text.WD_ALIGN_PARAGRAPH.LEFT
    )
    assert caption_style.font.italic is True and caption_style.font.bold is False
    assert caption_style.font.name == "Times New Roman"
    label_style = doc.styles["Caption Label"]
    assert label_style.font.bold is True and label_style.font.italic is False
    assert doc.styles["Figure"].paragraph_format.keep_with_next is True


def test_bib_markup_runs_convert_html_and_math_to_styles() -> None:
    from precis.export.docx import _bib_markup_runs

    runs = _bib_markup_runs(
        "{C$_{60}$} and C<sub>60</sub>: <i>In situ</i> <scp>iii</scp> Fe$^{3+}$ &amp; x"
    )
    text = "".join(t for t, _ in runs)
    assert text == "C60 and C60: In situ iii Fe3+ & x"
    styled = {t: st for t, st in runs if st}
    assert styled["60"] == frozenset({"sub"})
    assert styled["In situ"] == frozenset({"i"})
    assert styled["iii"] == frozenset({"sc"})
    assert styled["3+"] == frozenset({"sup"})
    for leak in ("$", "{", "<", "&amp;"):
        assert leak not in text


def test_reference_line_carries_venue_fields_and_no_double_period() -> None:
    from types import SimpleNamespace

    from precis.export.docx import _format_reference, _reference_runs

    paper = SimpleNamespace(
        id=3,
        slug="doe20",
        kind="paper",
        title="Charge transfer in {C$_{60}$} fields.",
        authors=[{"name": "Doe, Jane"}, {"name": "Roe & Sons"}],
        year=2020,
        meta={
            "venue": "J. Phys. <scp>iii</scp>",
            "volume": "12",
            "number": "4",
            "pages": "100-110",
        },
    )
    store = _RefStore({("paper", "doe20"): paper})
    warnings: list[str] = []
    line = _format_reference(store, "doe20", warnings)
    assert line == (
        "Doe, Jane; Roe & Sons (2020). Charge transfer in C60 fields."
        " J. Phys. iii 12(4), 100-110."
    )
    assert ".." not in line
    runs = _reference_runs(store, "doe20", warnings)
    assert ("60", frozenset({"sub"})) in runs
    assert ("J. Phys. ", frozenset({"i"})) in runs
    assert ("iii", frozenset({"i", "sc"})) in runs


def test_references_section_renders_sub_and_italic_runs(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from precis.export import docx as dx

    paper = SimpleNamespace(
        id=3,
        slug="doe20",
        kind="paper",
        title="On C<sub>60</sub>",
        authors=[{"name": "Doe, Jane"}],
        year=2020,
        meta={"journal": "Carbon", "volume": "7"},
    )
    ctx = dx._Ctx(
        store=_RefStore({("paper", "doe20"): paper}),
        known_handles=set(),
        cited=["doe20"],
    )
    doc = docx.Document()
    dx._append_references(doc, ctx)
    p = doc.paragraphs[-1]
    assert p.text == "[1]\tDoe, Jane (2020). On C60. Carbon 7."
    assert any(r.font.subscript and r.text == "60" for r in p.runs)
    assert any(r.italic and r.text == "Carbon" for r in p.runs)
    # A hanging indent from the style: every line but the first is indented.
    assert p.style.name == "Bibliography"
    fmt = p.style.paragraph_format
    assert fmt.left_indent == docx.shared.Inches(0.5)
    assert fmt.first_line_indent == docx.shared.Inches(-0.5)
    assert p.paragraph_format.left_indent is None


def test_endnote_reference_list_is_one_styled_paragraph_per_entry() -> None:
    """The cached ``EN.REFLIST`` text is one paragraph per reference in the
    ``EndNote Bibliography`` style, the field opening in the first and
    closing in the last (gr725245); it used to be one paragraph of breaks."""
    from types import SimpleNamespace

    from docx.oxml.ns import qn

    from precis.export import docx as dx

    papers = {
        ("paper", slug): SimpleNamespace(
            id=n,
            slug=slug,
            kind="paper",
            title=f"Title {n}",
            authors=[{"name": "Doe, Jane"}],
            year=2020 + n,
            meta={"journal": "Carbon", "volume": str(n)},
        )
        for n, slug in enumerate(["doe21", "doe22", "doe23"], start=1)
    }
    ctx = dx._Ctx(
        store=_RefStore(papers),
        known_handles=set(),
        cited=["doe21", "doe22", "doe23"],
        endnote=True,
    )
    doc = docx.Document()
    dx._append_references(doc, ctx)
    entries = doc.paragraphs[-3:]
    assert [p.style.name for p in entries] == ["EndNote Bibliography"] * 3
    assert [p.text for p in entries] == [
        "[1]\tDoe, Jane (2021). Title 1. Carbon 1.",
        "[2]\tDoe, Jane (2022). Title 2. Carbon 2.",
        "[3]\tDoe, Jane (2023). Title 3. Carbon 3.",
    ]
    hanging = doc.styles["EndNote Bibliography"].paragraph_format
    assert hanging.first_line_indent == docx.shared.Inches(-0.5)

    def kinds(p: Any) -> list[str]:
        return [fc.get(qn("w:fldCharType")) for fc in p._p.iter(qn("w:fldChar"))]

    assert kinds(entries[0]) == ["begin", "separate"]
    assert kinds(entries[1]) == []
    assert kinds(entries[2]) == ["end"]
    assert not list(doc.element.body.iter(qn("w:br")))


def test_reference_line_of_a_book_chapter_carries_its_volume() -> None:
    """A chapter in a numbered series prints the volume the record holds
    (gr725252): it used to read "In: IUTAM Bookseries, 109-121."."""
    from types import SimpleNamespace

    from precis.export.docx import _format_reference

    chapter = SimpleNamespace(
        id=692,
        slug="baowan09",
        kind="paper",
        title="Two Minimisation Approximations",
        authors=[{"name": "Baowan, Duangkamon"}],
        year=2009,
        meta={
            "entry_type": "book-chapter",
            "container_title": "IUTAM Bookseries",
            "volume": "13",
            "pages": "109-121",
        },
    )
    store = _RefStore({("paper", "baowan09"): chapter})
    assert _format_reference(store, "baowan09", []) == (
        "Baowan, Duangkamon (2009). Two Minimisation Approximations."
        " In: IUTAM Bookseries, vol. 13, 109-121."
    )
    chapter.meta.pop("volume")
    assert _format_reference(store, "baowan09", []).endswith(
        " In: IUTAM Bookseries, 109-121."
    )


def test_title_page_has_date_line_and_abstract_label(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """The prose between the title block and the first heading gets an
    "Abstract" label in the ``Abstract Title`` paragraph style (not a heading:
    no number, not in the outline) and sets in ``Abstract``. The label is
    deliberate in Word (gr725248, superseding gr491601 L4); the PDF has none."""
    from datetime import UTC, datetime

    pid = _new_draft_project(hub)
    draft.put(id="dab", title="Paper", project=pid)
    draft.put(id="dab", chunk_kind="paragraph", text="The abstract.", at={"last": True})
    draft.put(id="dab", chunk_kind="heading", text="Intro", at={"last": True})
    ref = hub.live_store.get_ref(kind="draft", id="dab")
    out = tmp_path / "dab.docx"
    export_docx(hub.live_store, ref, target_path=out)
    paras = docx.Document(str(out)).paragraphs
    texts = [p.text for p in paras]
    today = datetime.now(UTC)
    date_line = f"{today:%B} {today.day}, {today.year}"
    assert texts[1] == date_line  # right under the title (no byline here)
    assert texts[0] == "Paper"
    assert texts[2:4] == ["Abstract", "The abstract."]
    assert paras[2].style.name == "Abstract Title"
    assert paras[3].style.name == "Abstract"
    assert paras[2].style.font.bold is True
    assert paras[2].style.base_style.name == "Normal"
    assert paras[3].style.base_style.name == "Normal"
    assert not paras[2].style.name.startswith("Heading")
    assert texts.count("Abstract") == 1


def test_abstract_ends_at_keywords_and_first_heading(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    pid = _new_draft_project(hub)
    draft.put(id="dkw", title="Paper", project=pid)
    for text in ("First abstract para.", "Second one.", "KEYWORDS: a; b", "After."):
        draft.put(id="dkw", chunk_kind="paragraph", text=text, at={"last": True})
    draft.put(id="dkw", chunk_kind="heading", text="Intro", at={"last": True})
    draft.put(id="dkw", chunk_kind="paragraph", text="Body.", at={"last": True})
    ref = hub.live_store.get_ref(kind="draft", id="dkw")
    out = tmp_path / "dkw.docx"
    export_docx(hub.live_store, ref, target_path=out)
    styles = {p.text: p.style.name for p in docx.Document(str(out)).paragraphs}
    assert styles["Abstract"] == "Abstract Title"
    assert styles["First abstract para."] == "Abstract"
    assert styles["Second one."] == "Abstract"
    assert styles["KEYWORDS: a; b"] == "Normal"
    assert styles["After."] == "Normal"
    assert styles["Body."] == "Normal"


def test_no_abstract_label_without_prose_before_first_heading(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    pid = _new_draft_project(hub)
    draft.put(id="dna", title="Paper", project=pid)
    draft.put(id="dna", chunk_kind="heading", text="Intro", at={"last": True})
    draft.put(id="dna", chunk_kind="paragraph", text="Body.", at={"last": True})
    ref = hub.live_store.get_ref(kind="draft", id="dna")
    out = tmp_path / "dna.docx"
    export_docx(hub.live_store, ref, target_path=out)
    doc = docx.Document(str(out))
    assert "Abstract" not in [p.text for p in doc.paragraphs]
    assert "Abstract" not in [s.name for s in doc.styles]
    # a "Keywords:" paragraph straight after the title block is no abstract
    draft.put(id="dna2", title="Paper2", project=_new_draft_project(hub))
    draft.put(id="dna2", chunk_kind="paragraph", text="keywords: x", at={"last": True})
    ref2 = hub.live_store.get_ref(kind="draft", id="dna2")
    out2 = tmp_path / "dna2.docx"
    export_docx(hub.live_store, ref2, target_path=out2)
    assert "Abstract" not in [p.text for p in docx.Document(str(out2)).paragraphs]


def test_no_abstract_label_in_a_draft_without_sections(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """Opening prose is an abstract only when a section follows it: a note
    that is all paragraphs is not one long abstract."""
    pid = _new_draft_project(hub)
    draft.put(id="dnote", title="Note", project=pid)
    for text in ("First point.", "Second point."):
        draft.put(id="dnote", chunk_kind="paragraph", text=text, at={"last": True})
    ref = hub.live_store.get_ref(kind="draft", id="dnote")
    out = tmp_path / "dnote.docx"
    export_docx(hub.live_store, ref, target_path=out)
    doc = docx.Document(str(out))
    paras = {p.text: p.style.name for p in doc.paragraphs}
    assert "Abstract" not in paras
    assert paras["First point."] == "Normal" and paras["Second point."] == "Normal"


def _field_instrs(xml: str) -> list[str]:
    import re

    return re.findall(r"<w:instrText[^>]*>([^<]*)</w:instrText>", xml)


def test_footer_page_n_field_on_every_page(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """ "Page N" only: no page count, so a printout can leave out trailing
    pages (Reto, 2026-11-19)."""
    pid = _new_draft_project(hub)
    draft.put(id="dft", title="Paper", project=pid)
    ref = hub.live_store.get_ref(kind="draft", id="dft")
    out = tmp_path / "dft.docx"
    export_docx(hub.live_store, ref, target_path=out)
    doc = docx.Document(str(out))
    sec = doc.sections[0]
    assert sec.different_first_page_header_footer is True
    for footer in (sec.footer, sec.first_page_footer):
        par = footer.paragraphs[0]
        assert par.style.name == "Footer"
        assert par.alignment is None  # centred by the style, not the paragraph
        xml = par._p.xml
        assert [i.strip() for i in _field_instrs(xml)] == ["PAGE"]
        assert par.text == "Page 1"  # cached result
        assert xml.count('w:fldCharType="begin"') == 1
        assert xml.count('w:fldCharType="separate"') == 1
        assert xml.count('w:fldCharType="end"') == 1
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    assert doc.styles["Footer"].paragraph_format.alignment == WD_ALIGN_PARAGRAPH.CENTER
    assert "w:b/" not in sec.footer.paragraphs[0]._p.xml  # no direct formatting


def test_header_running_title_and_authors_after_first_page(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    from docx.enum.text import WD_TAB_ALIGNMENT

    pid = _new_draft_project(hub)
    draft.put(id="dhd", title="Surface chemistry: a long subtitle", project=pid)
    draft.edit(
        id="dhd",
        authors=[{"name": "Doe, Jane"}, {"name": "Roe, John"}, {"name": "X, Y"}],
    )
    ref = hub.live_store.get_ref(kind="draft", id="dhd")
    out = tmp_path / "dhd.docx"
    export_docx(hub.live_store, ref, target_path=out)
    doc = docx.Document(str(out))
    sec = doc.sections[0]
    par = sec.header.paragraphs[0]
    assert par.style.name == "Header"
    assert par.text == "Surface chemistry\tDoe et al."
    # the first-page header stays empty
    assert sec.first_page_header.paragraphs[0].text == ""
    tabs = list(doc.styles["Header"].paragraph_format.tab_stops)
    assert [(t.alignment, t.position.inches) for t in tabs] == [
        (WD_TAB_ALIGNMENT.RIGHT, 6.5)
    ]


def test_running_title_cuts() -> None:
    from precis.export.docx import running_title

    assert running_title("Short title") == "Short title"
    assert running_title("Main part: and a subtitle") == "Main part"
    assert running_title("Main part - and a subtitle") == "Main part"
    assert running_title("Main part \u2013 sub") == "Main part"
    assert running_title("Main part \u2014 sub") == "Main part"
    assert running_title("Well-known fact about CO<sub>2</sub>") == (
        "Well-known fact about CO2"
    )
    long = " ".join(["word"] * 30)
    cut = running_title(long)
    assert cut.endswith("\u2026") and len(cut) <= 61
    assert cut[:-1] == " ".join(["word"] * 12)  # whole words, 59 chars
    assert running_title(None) == ""


def test_running_title_of_a_title_that_starts_with_a_separator() -> None:
    from precis.export.docx import running_title

    assert running_title(": a subtitle on its own") == ": a subtitle on its own"
    assert running_title("\u2014 on its own") == "\u2014 on its own"
    assert running_title("- on its own") == "- on its own"
    long = ": " + " ".join(["word"] * 30)
    cut = running_title(long)
    assert cut.endswith("\u2026") and len(cut) <= 61  # the 60-character rule


def test_running_authors_forms() -> None:
    from precis.export.docx import running_authors
    from precis.utils.authors import build_byline

    def run(*names: str) -> str:
        return running_authors(build_byline([{"name": n} for n in names]))

    assert run() == ""
    assert run("Lipin, Raju") == "Lipin"
    assert run("Lipin, Raju", "Doe, Jane") == "Lipin and Doe"
    assert run("Lipin, Raju", "Doe, Jane", "Roe, John") == "Lipin et al."


def test_byline_affiliation_number_then_thin_space_then_orcid() -> None:
    from precis.export.docx import _render_byline
    from precis.utils.authors import build_byline

    doc = docx.Document()
    doc.add_heading("Title", level=0)
    _render_byline(
        doc,
        build_byline(
            [
                {
                    "name": "Lipin, Raju",
                    "affiliation": "MIT",
                    "orcid": "0000-0002-1825-0097",
                },
                {"name": "Roe, John", "affiliation": "Caltech"},
            ]
        ),
    )
    names_p = doc.paragraphs[1]
    assert names_p.text == "Raju Lipin1\u2009iD, John Roe2"
    runs = names_p.runs
    sup = next(r for r in runs if r.text == "1")
    assert sup.font.superscript is True
    thin = next(r for r in runs if r.text == "\u2009")
    assert not thin.font.superscript
    # order in the XML: number run, thin-space run, then the iD hyperlink
    xml = names_p._p.xml
    assert xml.index(">1<") < xml.index("\u2009") < xml.index(">iD<")


def test_nested_layout_numbers_sections_from_one(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """title -> Introduction -> Scope displays "1" and "1.1", and a
    cross-ref to Scope prints the same number."""
    import re

    pid = _new_draft_project(hub)
    draft.put(id="dnest", title="Nested", project=pid)
    ref = hub.live_store.get_ref(kind="draft", id="dnest")
    assert ref is not None
    title_dc = hub.live_store.drafts.reading_order(ref.id)[0].handle
    intro = draft.put(
        id="dnest",
        chunk_kind="heading",
        text="Introduction",
        at={"into": title_dc, "last": True},
    )
    intro_m = re.search(r"dc\d+", intro.body)
    assert intro_m is not None
    intro_h = intro_m.group(0)
    scope = draft.put(
        id="dnest",
        chunk_kind="heading",
        text="Scope",
        at={"into": intro_h, "last": True},
    )
    scope_m = re.search(r"dc\d+", scope.body)
    assert scope_m is not None
    scope_h = scope_m.group(0)
    next_sec = draft.put(
        id="dnest",
        chunk_kind="heading",
        text="Next",
        at={"into": title_dc, "last": True},
    )
    next_m = re.search(r"dc\d+", next_sec.body)
    assert next_m is not None
    draft.put(
        id="dnest",
        chunk_kind="paragraph",
        text=f"See [{scope_h}].",
        at={"into": next_m.group(0), "last": True},
    )
    out = tmp_path / "dnest.docx"
    export_docx(hub.live_store, ref, target_path=out)
    paras = docx.Document(str(out)).paragraphs
    texts = [p.text for p in paras]
    assert "Nested" in texts and "1 Introduction" in texts and "1.1 Scope" in texts
    assert "See Section 1.1." in texts
    head = next(p for p in paras if p.text == "1.1 Scope")
    assert head.style.name == "Heading 2"


def test_numbers_and_cross_references_are_word_fields(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """gr725244: caption and equation numbers are ``SEQ`` fields, every
    number is bookmarked ``_Ref_<dc>``, every cross-reference is a ``REF``
    field to an existing bookmark, and the visible text is exactly what the
    static export printed."""
    pytest.importorskip("latex2mathml")
    import re
    import zipfile

    from docx.oxml.ns import qn

    def handle(resp) -> str:
        m = re.search(r"dc\d+", resp.body)
        assert m is not None
        return m.group(0)

    pid = _new_draft_project(hub)
    draft.put(id="dfld", title="Fields", project=pid)
    ref = hub.live_store.get_ref(kind="draft", id="dfld")
    assert ref is not None
    drafts = hub.live_store.drafts
    title_h = drafts.reading_order(ref.id)[0].handle
    intro = handle(
        draft.put(
            id="dfld",
            chunk_kind="heading",
            text="Intro",
            at={"into": title_h, "last": True},
        )
    )
    methods = handle(
        draft.put(
            id="dfld",
            chunk_kind="heading",
            text="Methods",
            at={"into": title_h, "last": True},
        )
    )
    fig = drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="figure",
        text="First widget.",
        at={"into": methods, "last": True},
        split=False,
    )[0]
    tbl = handle(
        draft.put(
            id="dfld",
            chunk_kind="table",
            table={"header": ["a"], "rows": [[1]]},
            caption="Legend",
            at={"into": methods, "last": True},
        )
    )
    eq = handle(
        draft.put(
            id="dfld",
            chunk_kind="paragraph",
            text="$$E = mc^2$$",
            at={"into": methods, "last": True},
        )
    )
    # In Intro, so every target but the section-1 one comes later (a forward
    # reference); [methods] is another section than the citing one.
    draft.put(
        id="dfld",
        chunk_kind="paragraph",
        text=(
            f"See [{fig.dc}], [{tbl}], [{methods}] and [{eq}]. [{eq}] holds."
            f" Above, [{intro}] is this section."
        ),
        at={"into": intro, "last": True},
    )
    out = tmp_path / "dfld.docx"
    export_docx(hub.live_store, ref, target_path=out)
    doc = docx.Document(str(out))
    texts = [p.text for p in doc.paragraphs]

    # visible text: as before fields existed
    assert (
        "See Figure 1, Table 1, Section 2, and Eq. (1). Equation (1) holds."
        " Above, above is this section." in texts
    )
    assert "Figure 1: First widget." in texts
    assert "Table 1: Legend" in texts
    assert "1 Intro" in texts and "2 Methods" in texts
    assert any(t.endswith("(1)") and t.startswith("\t") for t in texts)

    body = doc.element.body
    instrs = [i.text.strip() for i in body.iter(qn("w:instrText"))]
    assert r"SEQ Figure \* ARABIC" in instrs
    assert r"SEQ Table \* ARABIC" in instrs
    assert r"SEQ Equation \* ARABIC" in instrs

    # bookmarks: unique legal names and ids
    marks = list(body.iter(qn("w:bookmarkStart")))
    names = [b.get(qn("w:name")) for b in marks]
    ids = [b.get(qn("w:id")) for b in marks]
    assert len(set(names)) == len(names) and len(set(ids)) == len(ids)
    assert all(re.fullmatch(r"_Ref_\w+", n) and len(n) <= 40 for n in names)
    assert all(i.isdigit() for i in ids)
    ends = [b.get(qn("w:id")) for b in body.iter(qn("w:bookmarkEnd"))]
    assert sorted(ends) == sorted(ids)
    assert {f"_Ref_{d}" for d in (fig.dc, tbl, eq, intro, methods)} <= set(names)

    # every REF names an existing bookmark; Section 2 points at its heading
    refs = [i for i in instrs if i.startswith("REF ")]
    assert refs and all(r.split()[1] in names for r in refs)
    # MERGEFORMAT: an update keeps the citing text's formatting, not the
    # bold caption label's or the heading's.
    tail = r"\h \* MERGEFORMAT"
    assert refs.count(f"REF _Ref_{methods} {tail}") == 1
    assert refs.count(f"REF _Ref_{eq} {tail}") == 2
    assert f"REF _Ref_{fig.dc} {tail}" in refs and f"REF _Ref_{tbl} {tail}" in refs

    # each bookmark opens and closes in one paragraph, start before end
    for p in body.iter(qn("w:p")):
        kids = list(p)
        opened = [k.get(qn("w:id")) for k in kids if k.tag == qn("w:bookmarkStart")]
        closed = [k.get(qn("w:id")) for k in kids if k.tag == qn("w:bookmarkEnd")]
        assert opened == closed
        for k in kids:
            if k.tag == qn("w:bookmarkStart"):
                after = kids[kids.index(k) + 1 :]
                assert any(
                    a.tag == qn("w:bookmarkEnd")
                    and a.get(qn("w:id")) == k.get(qn("w:id"))
                    for a in after
                )

    # the cached number of a reference is the cached number of its target
    def cached_after(instr_el: Any) -> str:
        run = instr_el.getparent().getnext().getnext()  # separate, then result
        return "".join(t.text or "" for t in run.iter(qn("w:t")))

    cached = {i.text.strip(): cached_after(i) for i in body.iter(qn("w:instrText"))}
    assert cached[r"SEQ Figure \* ARABIC"] == cached[f"REF _Ref_{fig.dc} {tail}"]
    assert cached[r"SEQ Table \* ARABIC"] == cached[f"REF _Ref_{tbl} {tail}"]
    assert cached[r"SEQ Equation \* ARABIC"] == cached[f"REF _Ref_{eq} {tail}"]

    # the figure caption: label word, field and colon all ``Caption Label``;
    # the bookmark holds the field only, not the word
    cap = next(p for p in doc.paragraphs if p.text == "Figure 1: First widget.")
    label_runs = cap.runs[:7]
    assert all(r.style.name == "Caption Label" for r in label_runs)
    kids = [c.tag for c in cap._p]
    start = kids.index(qn("w:bookmarkStart"))
    end = kids.index(qn("w:bookmarkEnd"))
    assert end - start == 6  # start, five field runs, end
    assert cap.runs[0].text == "Figure "
    # a heading's typed number is bookmarked on its own
    head = next(p for p in doc.paragraphs if p.text == "2 Methods")
    kids = list(head._p)
    mark = next(c for c in kids if c.tag == qn("w:bookmarkStart"))
    inner = mark.getnext()
    assert inner is not None and inner.text == "2"
    assert inner.getnext().tag == qn("w:bookmarkEnd")

    with zipfile.ZipFile(out) as z:
        assert b"updateFields" not in z.read("word/settings.xml")


def test_adjacent_cross_references_print_as_one_run_of_fields(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """gr725244: references separated by whitespace only print as the PDF's
    ``\\cref{a,b}`` does ("Sections 1 and 2", "Figures 1 to 3", "Sections 2
    and 3 and Figure 1"), every number its own ``REF`` field to an existing
    bookmark; a range has fields for its first and last target only; a
    reference into the citing section ("above") or an "or" ends the run;
    commas and "and" between references merge them."""
    pytest.importorskip("latex2mathml")
    import re

    from docx.oxml.ns import qn

    def handle(resp) -> str:
        m = re.search(r"dc\d+", resp.body)
        assert m is not None
        return m.group(0)

    pid = _new_draft_project(hub)
    draft.put(id="drun", title="Runs", project=pid)
    ref = hub.live_store.get_ref(kind="draft", id="drun")
    assert ref is not None
    drafts = hub.live_store.drafts
    title_h = drafts.reading_order(ref.id)[0].handle
    sections = {}
    for name in ("Intro", "Methods", "Results", "Discussion"):
        sections[name] = handle(
            draft.put(
                id="drun",
                chunk_kind="heading",
                text=name,
                at={"into": title_h, "last": True},
            )
        )
    figs = [
        drafts.add_chunks(
            ref_id=ref.id,
            chunk_kind="figure",
            text=f"Widget {n}.",
            at={"into": sections["Results"], "last": True},
            split=False,
        )[0].dc
        for n in (1, 2, 3)
    ]
    intro, methods, results, discussion = (
        sections[n] for n in ("Intro", "Methods", "Results", "Discussion")
    )
    f1, f2, f3 = figs
    sources = [
        f"See [{intro}] [{methods}].",
        f"See [{f3}] [{f1}][{f2}].",
        f"See [{methods}] [{f1}] [{results}].",
        f"See [{intro}] [{discussion}] [{methods}].",  # the own section breaks it
        f"See [{intro}], [{methods}] and [{results}].",  # commas and "and" join
        f"See [{intro}] or [{methods}].",  # "or" does not
    ]
    for source in sources:
        draft.put(
            id="drun",
            chunk_kind="paragraph",
            text=source,
            at={"into": discussion, "last": True},
        )
    out = tmp_path / "drun.docx"
    export_docx(hub.live_store, ref, target_path=out)
    doc = docx.Document(str(out))
    body = doc.element.body
    names = {b.get(qn("w:name")) for b in body.iter(qn("w:bookmarkStart"))}

    def para(prefix: str) -> Any:
        return next(p for p in doc.paragraphs if p.text.startswith(prefix))

    def fields(p: Any) -> list[str]:
        return [i.text.strip() for i in p._p.iter(qn("w:instrText"))]

    tail = r"\h \* MERGEFORMAT"
    sec_head = {n: f"REF _Ref_{h} {tail}" for n, h in sections.items()}

    p = para("See Sections 1 and 2")
    assert p.text == "See Sections 1 and 2."
    assert fields(p) == [sec_head["Intro"], sec_head["Methods"]]

    p = para("See Figures")
    assert p.text == "See Figures 1 to 3."
    assert fields(p) == [f"REF _Ref_{f1} {tail}", f"REF _Ref_{f3} {tail}"]

    p = para("See Sections 2")
    assert p.text == "See Sections 2 and 3 and Figure 1."
    assert fields(p) == [
        sec_head["Methods"],
        sec_head["Results"],
        f"REF _Ref_{f1} {tail}",
    ]

    p = para("See Section 1 above")
    assert p.text == "See Section 1 above Section 2."
    assert fields(p) == [sec_head["Intro"], sec_head["Methods"]]

    p = para("See Sections 1 to 3")
    assert p.text == "See Sections 1 to 3."
    assert fields(p) == [sec_head["Intro"], sec_head["Results"]]

    p = para("See Section 1 or")
    assert p.text == "See Section 1 or Section 2."
    assert fields(p) == [sec_head["Intro"], sec_head["Methods"]]

    every = [i.text.strip() for i in body.iter(qn("w:instrText"))]
    refs = [i for i in every if i.startswith("REF ")]
    assert refs and all(r.split()[1] in names for r in refs)
    # a field is whole: begin, instruction, separate, result, end
    for p in body.iter(qn("w:p")):
        marks = [f.get(qn("w:fldCharType")) for f in p.iter(qn("w:fldChar"))]
        assert marks == ["begin", "separate", "end"] * (len(marks) // 3)


def test_merged_run_survives_a_missing_bookmark_and_a_span_between() -> None:
    """A reference to a withheld target loses its field, not its number
    (:func:`docx._unlink_dangling_refs`); math, a link or any other prose
    between two references keeps them apart."""
    from docx.oxml.ns import qn

    from precis.export import docx as dx

    ctx = dx._Ctx(
        store=None,
        known_handles=set(),
        xref={"dc1": ("Figure", "1"), "dc2": ("Figure", "2"), "dc3": ("Figure", "3")},
        xref_types={"dc1": "figure", "dc2": "figure", "dc3": "figure"},
        dc_handles={"dc1", "dc2", "dc3"},
    )
    d = docx.Document()
    par = d.add_paragraph()
    dx._render_inline("See [dc1] [dc2] [dc3].", ctx, par)
    assert par.text == "See Figures 1 to 3."
    instrs = [i.text.strip() for i in par._p.iter(qn("w:instrText"))]
    assert instrs == [
        r"REF _Ref_dc1 \h \* MERGEFORMAT",
        r"REF _Ref_dc3 \h \* MERGEFORMAT",
    ]
    dx._unlink_dangling_refs(d)  # no bookmarks were written in this document
    assert par.text == "See Figures 1 to 3."
    assert not list(par._p.iter(qn("w:instrText")))
    assert not list(par._p.iter(qn("w:fldChar")))

    ctx.xref_types.pop("dc2")  # a target whose type is unknown prints alone
    assert _render_text("See [dc1] [dc2].", ctx) == "See Figure 1 Figure 2."
    ctx.xref_types["dc2"] = "figure"
    shown = _render_text("See [dc1] $x$ [dc2].", ctx)  # the math span is OMML
    assert "Figures" not in shown and "Figure 1" in shown and "Figure 2" in shown


def test_ref_to_a_missing_bookmark_keeps_plain_number() -> None:
    from docx.oxml.ns import qn

    from precis.export import docx as dx

    d = docx.Document()
    par = d.add_paragraph()
    par.add_run("see Figure ")
    dx._add_field(par, r"REF _Ref_dc9 \h \* MERGEFORMAT", "3")
    par.add_run(" here")
    dx._unlink_dangling_refs(d)
    assert par.text == "see Figure 3 here"
    assert not list(d.element.body.iter(qn("w:instrText")))
    assert not list(d.element.body.iter(qn("w:fldChar")))


def test_unrecoverable_table_keeps_its_number_field() -> None:
    """A table chunk whose table cannot be recovered prints as a paragraph,
    but the walk numbered it: it keeps "Table N" with the ``SEQ`` field and
    the bookmark, so a field update does not renumber the later tables."""
    from docx.oxml.ns import qn

    from precis.export import docx as dx

    ctx = dx._Ctx(store=_RefStore({}), known_handles=set())
    ctx.xref["dc7"] = ("Table", "1")
    d = docx.Document()
    chunk = SimpleNamespace(dc="dc7", text="not a table", meta=None)
    dx._render_table(d, chunk, ctx)
    assert [p.text for p in d.paragraphs][-2:] == ["Table 1", "not a table"]
    body = d.element.body
    assert [i.text.strip() for i in body.iter(qn("w:instrText"))] == [
        r"SEQ Table \* ARABIC"
    ]
    assert [b.get(qn("w:name")) for b in body.iter(qn("w:bookmarkStart"))] == [
        "_Ref_dc7"
    ]


def test_field_after_a_dropped_mark_is_not_cleared() -> None:
    """A cross-reference number ends a paragraph's runs with the field's end
    mark; trimming the prose before a dropped citation must leave it."""
    from docx.oxml.ns import qn

    from precis.export import docx as dx

    d = docx.Document()
    par = d.add_paragraph()
    par.add_run("Figure ")
    dx._add_field(par, r"REF _Ref_dc9 \h", "3")
    dx._rstrip_last_run(par)
    assert par.text == "Figure 3"
    assert len(list(par._p.iter(qn("w:fldChar")))) == 3


def test_crossref_into_own_section_prints_above_or_below(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """gr725243: a ``[dc]`` into the citing chunk's own section or an
    ancestor prints "above" / "below"; other sections keep "section N"."""
    import re

    def handle(resp) -> str:
        m = re.search(r"dc\d+", resp.body)
        assert m is not None
        return m.group(0)

    pid = _new_draft_project(hub)
    draft.put(id="dabove", title="Own section", project=pid)
    ref = hub.live_store.get_ref(kind="draft", id="dabove")
    assert ref is not None
    title_dc = hub.live_store.drafts.reading_order(ref.id)[0].handle
    intro = handle(
        draft.put(
            id="dabove",
            chunk_kind="heading",
            text="Introduction",
            at={"into": title_dc, "last": True},
        )
    )
    setup = handle(
        draft.put(
            id="dabove",
            chunk_kind="paragraph",
            text="The setup.",
            at={"into": intro, "last": True},
        )
    )
    scope = handle(
        draft.put(
            id="dabove",
            chunk_kind="heading",
            text="Scope",
            at={"into": intro, "last": True},
        )
    )
    later = handle(
        draft.put(
            id="dabove",
            chunk_kind="paragraph",
            text="The later claim.",
            at={"into": scope, "last": True},
        )
    )
    other = handle(
        draft.put(
            id="dabove",
            chunk_kind="heading",
            text="Other",
            at={"into": title_dc, "last": True},
        )
    )
    draft.put(
        id="dabove",
        chunk_kind="paragraph",
        text=(
            f"(see [{setup}]) and (see [{later}]). [{setup}] is the setup;"
            f" [{other}] is elsewhere."
        ),
        at={"into": scope, "first": True},
    )
    out = tmp_path / "dabove.docx"
    export_docx(hub.live_store, ref, target_path=out)
    texts = [p.text for p in docx.Document(str(out)).paragraphs]
    assert (
        "(see above) and (see below). Above is the setup; Section 2 is elsewhere."
        in texts
    )


def test_endnote_field_record_quality(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """gr491600: RecNum carries the corpus ref id (stable across exports),
    the cached in-text display is numeric ``[n]`` matching the numbered
    bibliography, EN.Layout names the mapped output style, and
    EN.Libraries lists resolved ref ids only."""
    import zipfile

    store = hub.live_store
    _seed_paper(store, "q1", "Quality paper", 2001)
    pref = store.get_ref(kind="paper", id="q1")
    assert pref is not None
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="rq", title="T", project=pid)
    draft.put(
        id="rq", chunk_kind="paragraph", text="Claim [§q1] holds.", at={"last": True}
    )
    ref = store.get_ref(kind="draft", id="rq")
    out = tmp_path / "rq.docx"
    export_docx(store, ref, target_path=out, citations="endnote")
    with zipfile.ZipFile(out) as z:
        body = z.read("word/document.xml").decode("utf-8")
        settings = z.read("word/settings.xml").decode("utf-8")
    assert f"&lt;RecNum&gt;{pref.id}&lt;/RecNum&gt;" in body
    assert f"&lt;rec-number&gt;{pref.id}&lt;/rec-number&gt;" in body
    assert "&lt;DisplayText&gt;[1]&lt;/DisplayText&gt;" in body
    assert "Style&gt;Numbered" in settings  # numeric-comp → EndNote "Numbered"
    assert f"&lt;item&gt;{pref.id}&lt;/item&gt;" in settings


def test_endnote_missing_year_warns(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """A cited paper with no year still exports (empty ``<Year/>``) but
    reports a warning instead of failing silently."""
    store = hub.live_store
    store.insert_ref(kind="paper", slug="noyr", title="Yearless", provider="manual")
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="ny", title="T", project=pid)
    draft.put(
        id="ny", chunk_kind="paragraph", text="Ref [§noyr] here.", at={"last": True}
    )
    ref = store.get_ref(kind="draft", id="ny")
    res = export_docx(store, ref, target_path=tmp_path / "ny.docx", citations="endnote")
    assert any("noyr" in w and "year" in w for w in res.warnings)


def test_heading_and_table_header_citations_number(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """gr491601 L2: heading text and table header cells go through the
    inline grammar — a citation inside them numbers against the shared
    bibliography (it used to print literally and stay unnumbered)."""
    store = hub.live_store
    _seed_paper(store, "h1", "Heading paper", 2001)
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="hh", title="T", project=pid)
    draft.put(
        id="hh", chunk_kind="heading", text="Results from [§h1]", at={"last": True}
    )
    draft.put(
        id="hh",
        chunk_kind="table",
        table={"header": ["Metric [§h1]", "Value"], "rows": [["x", "1"]]},
        at={"last": True},
    )
    ref = store.get_ref(kind="draft", id="hh")
    out = tmp_path / "hh.docx"
    res = export_docx(store, ref, target_path=out)
    assert res.cited_slugs == ["h1"]
    doc = docx.Document(str(out))
    heading = next(p for p in doc.paragraphs if "Results" in p.text)
    assert "[1]" in heading.text
    hdr_cell = doc.tables[0].rows[0].cells[0].paragraphs[0]
    assert "[1]" in hdr_cell.text
    assert "Heading paper" in "\n".join(p.text for p in doc.paragraphs)


def test_no_spurious_abstract_heading(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """gr491601 L4 / gr725248: prose between title and first heading gets no
    "Abstract" HEADING (no number, no outline entry); the Word label is a
    plain ``Abstract Title`` paragraph and the PDF has none."""
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="ab", title="My Title", project=pid)
    draft.put(
        id="ab", chunk_kind="paragraph", text="Abstract body text.", at={"last": True}
    )
    ref = hub.live_store.get_ref(kind="draft", id="ab")
    out = tmp_path / "ab.docx"
    export_docx(hub.live_store, ref, target_path=out)
    doc = docx.Document(str(out))
    assert not [
        p
        for p in doc.paragraphs
        if p.text == "Abstract" and p.style.name != "Abstract Title"
    ]
    assert "Abstract body text." in [p.text for p in doc.paragraphs]


def test_abbrev_long_form_containing_short_collapses_once(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """gr491599 H3: an authored ``long (SHORT)`` whose long form contains
    another defined short collapses to the single span — the inner short
    must not be consumed first and double the expansion."""
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="fc", title="T", project=pid)
    draft.put(
        id="fc",
        chunk_kind="term",
        text="carbon nanobud",
        meta={"short": "CNB"},
    )
    draft.put(
        id="fc",
        chunk_kind="term",
        text="functionalized CNB",
        meta={"short": "FCNB"},
    )
    draft.put(
        id="fc",
        chunk_kind="paragraph",
        text="A functionalized CNB (FCNB) grows. CNB films too.",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="fc")
    out = tmp_path / "fc.docx"
    export_docx(hub.live_store, ref, target_path=out)
    body = next(p.text for p in docx.Document(str(out)).paragraphs if "grows" in p.text)
    # The authored span collapses once; the later bare "CNB" is its own
    # first use and expands like the PDF does.
    assert body == "A functionalized CNB (FCNB) grows. Carbon nanobud (CNB) films too."


def test_abbrev_definitions_inside_bold_expand(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """gr491599 L3: a first use inside **bold** still expands (the PDF
    defines inside bold too), not a skipped span."""
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="bd", title="T", project=pid)
    draft.put(
        id="bd",
        chunk_kind="term",
        text="metal-organic framework",
        meta={"short": "MOF"},
    )
    draft.put(
        id="bd",
        chunk_kind="paragraph",
        text="Uptake in **MOF films** is high.",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="bd")
    out = tmp_path / "bd.docx"
    export_docx(hub.live_store, ref, target_path=out)
    doc = docx.Document(str(out))
    para = next(p for p in doc.paragraphs if "Uptake" in p.text)
    assert "metal-organic framework" in para.text
    bold = [r.text for r in para.runs if r.bold]
    assert bold == ["metal-organic framework (MOF)", " films"]


def test_endnote_si_cite_carries_suffix(monkeypatch) -> None:
    """An SI cite in endnote mode gets its own field: ``<Suffix> (SI)</Suffix>``
    before ``<DisplayText>``, DisplayText and the cached text ``[n] (SI)``."""
    import docx as _docx
    from lxml import etree

    from precis.export import docx as dx

    ctx = _si_docx_ctx(monkeypatch, with_parent=True)
    ctx.endnote = True
    monkeypatch.setattr(
        dx,
        "_resolve_source",
        lambda _s, slug: {
            "rec_number": 1,
            "title": "Parent",
            "authors": ["Doe, J."],
            "year": 2024,
            "slug": slug,
        },
    )
    para = _docx.Document().add_paragraph()
    dx._cite("parent24si", ctx, para)
    dx._flush_cites(ctx, para)
    assert ctx.cited == ["parent24"]
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    instrs = [e.text or "" for e in para._p.iter(w + "instrText")]
    assert len(instrs) == 1
    assert instrs[0].strip().startswith("ADDIN EN.CITE <EndNote>")
    payload = instrs[0].strip()[len("ADDIN EN.CITE ") :]
    cite = etree.fromstring(payload.encode("utf-8"))[0]
    assert cite.findtext("Suffix") == " (SI)"
    assert cite.findtext("DisplayText") == "[1] (SI)"
    tags = [c.tag for c in cite]
    assert tags.index("Suffix") < tags.index("DisplayText")
    texts = [e.text for e in para._p.iter(w + "t")]
    assert "[1] (SI)" in texts


def _si_next_to_plain_ctx(monkeypatch, *, endnote: bool):
    """gr725246: the SI parent is cited first (number 1), ``other14`` second
    (number 2); the paragraph then cites ``other14`` and the SI record back
    to back."""
    from types import SimpleNamespace

    from precis.export import docx as dx

    ctx = _si_docx_ctx(monkeypatch, with_parent=True)
    ctx.store._refs[("paper", "other14")] = SimpleNamespace(
        id=3, slug="other14", kind="paper", pdf_role=None
    )
    ctx.endnote = endnote
    ctx.cite_number("parent24")
    ctx.cite_number("other14")
    monkeypatch.setattr(
        dx,
        "_resolve_source",
        lambda _s, slug: {
            "rec_number": 1 if slug == "parent24" else 2,
            "title": slug,
            "authors": ["Doe, J."],
            "year": 2024,
            "slug": slug,
        },
    )
    return ctx


def test_si_cite_next_to_plain_cite_is_one_group(monkeypatch) -> None:
    """gr725246: ``[14][9] (SI)`` becomes one group, the SI mark on its own
    number, ascending: ``[1 (SI), 2]``."""
    import docx as _docx

    from precis.export import docx as dx

    ctx = _si_next_to_plain_ctx(monkeypatch, endnote=False)
    para = _docx.Document().add_paragraph()
    dx._cite("other14", ctx, para)
    dx._cite("parent24si", ctx, para)
    dx._flush_cites(ctx, para)
    assert para.text == "[1 (SI), 2]"


def test_endnote_si_cite_next_to_plain_cite_is_one_field(monkeypatch) -> None:
    """gr725246: EndNote mode: ONE ``ADDIN EN.CITE`` field, two ``<Cite>``
    elements, ``<Suffix> (SI)</Suffix>`` on the SI one only."""
    import docx as _docx
    from lxml import etree

    from precis.export import docx as dx

    ctx = _si_next_to_plain_ctx(monkeypatch, endnote=True)
    para = _docx.Document().add_paragraph()
    dx._cite("other14", ctx, para)
    dx._cite("parent24si", ctx, para)
    dx._flush_cites(ctx, para)
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    instrs = [e.text or "" for e in para._p.iter(w + "instrText")]
    assert len(instrs) == 1
    payload = instrs[0].strip()[len("ADDIN EN.CITE ") :]
    cites = list(etree.fromstring(payload.encode("utf-8")))
    assert [c.findtext("Suffix") for c in cites] == [" (SI)", None]
    assert [c.findtext("DisplayText") for c in cites] == ["[1] (SI)", "[2]"]
    assert [e.text for e in para._p.iter(w + "t")] == ["[1 (SI), 2]"]


def test_merged_cite_numbers_si_keeps_mark_on_its_number() -> None:
    """gr725246: ascending, the SI mark on its own number, ranges only
    among plain numbers."""
    from precis.export._cite_ids import merged_cite_numbers_si

    assert merged_cite_numbers_si([(14, False), (9, True)]) == "[9 (SI), 14]"
    assert (
        merged_cite_numbers_si([(1, False), (2, False), (3, False), (4, True)])
        == "[1\u20133, 4 (SI)]"
    )
    assert merged_cite_numbers_si([(1, False), (2, True), (3, False)]) == (
        "[1, 2 (SI), 3]"
    )


def test_si_cite_plain_mode_keeps_si_note(monkeypatch) -> None:
    import docx as _docx

    from precis.export import docx as dx

    ctx = _si_docx_ctx(monkeypatch, with_parent=True)
    assert not ctx.endnote
    para = _docx.Document().add_paragraph()
    dx._cite("parent24si", ctx, para)
    dx._flush_cites(ctx, para)
    assert para.text == "[1] (SI)"


def test_bare_handles_in_prose_are_replaced() -> None:
    """gr493181: ``dr173020``-style tokens never reach the reader — resolved
    to the record title, else the kind name plus a warning. DOIs and
    chemistry tokens stay."""
    from types import SimpleNamespace

    from precis.export import docx as dx

    refs = {173020: SimpleNamespace(meta={}, title="Carbon Nanobuds Review")}
    ctx = dx._Ctx(
        store=_FindingFake(refs),
        known_handles=set(),
        doi_links=False,
        library_links=False,
        library_label="x",
        library_search_url="x",
    )
    doi = "10.1002/j.2168-0159.2014.tb00055.x"
    out = _render_text(
        f"Read dr173020's draft and td459589/catpath; see {doi} and co2 on pt111.",
        ctx,
    )
    assert "Carbon Nanobuds Review's draft" in out
    assert "todo/catpath" in out
    assert doi in out
    assert "co2" in out and "pt111" in out
    assert not re.search(r"\b[a-z]{2}\d{4,}\b", out.replace(doi, ""))
    assert any("td459589" in w for w in ctx.warnings)


def test_bare_citable_handle_in_code_and_math_not_cited() -> None:
    """td607394: a ``pa<id>`` inside a code or math span is span content,
    not a citation — matching it split `` `pa57151` `` at the handle,
    minting a ``[n]`` cite out of code. Only the prose occurrence cites."""
    from types import SimpleNamespace

    from precis.export import docx as dx

    store = SimpleNamespace(
        resolve_handle=lambda h: (
            SimpleNamespace(public_id="kim26") if h == "pa57151" else None
        )
    )
    ctx = dx._Ctx(
        store=store,
        known_handles=set(),
        doi_links=False,
        library_links=False,
        library_label="x",
        library_search_url="x",
    )
    para = docx.Document().add_paragraph()
    dx._render_inline(
        "handle `pa57151` and term $x_{pa57151}$ stay put; pa57151 cites.",
        ctx,
        para,
    )
    assert ctx.cited == ["kim26"]  # only the prose occurrence cites
    assert "[1]" in para.text
    code_runs = [r for r in para.runs if r.font.name == "Consolas"]
    assert any(r.text == "pa57151" for r in code_runs)


def test_adsorbate_star_and_escaped_star_not_italic() -> None:
    """gr674905: ``*NO``/``H*`` adsorbate stars and the ``\\*`` escape are
    literal in docx too — same rule as LaTeX (``utils/adsorbate_star``);
    a real ``*word*`` still italicises."""
    from precis.export import docx as dx

    ctx = dx._Ctx(
        store=None,
        known_handles=set(),
        doi_links=False,
        library_links=False,
        library_label="x",
        library_search_url="x",
    )
    para = docx.Document().add_paragraph()
    dx._render_inline(
        r"*NO + O* adsorbates, a *word* italic, and \*escaped\* stars.",
        ctx,
        para,
    )
    assert [r.text for r in para.runs if r.italic] == ["word"]
    assert "*NO" in para.text and "O*" in para.text
    assert "\\*" not in para.text and "*escaped*" in para.text


def _back_matter_headings(draft: DraftHandler, hub: Hub, tmp_path: Path) -> list[str]:
    _seed_paper(hub.live_store, "bm1", "Back matter paper", 2020)
    pid = _new_draft_project(hub)
    draft.put(id="dbm", title="T", project=pid)
    draft.put(
        id="dbm", chunk_kind="term", text="carbon nanotube", meta={"short": "CNT"}
    )
    draft.put(
        id="dbm",
        chunk_kind="paragraph",
        text="We study CNT [§bm1] here.",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="dbm")
    out = tmp_path / "dbm.docx"
    export_docx(hub.live_store, ref, target_path=out)
    return [
        p.text
        for p in docx.Document(str(out)).paragraphs
        if p.style is not None and p.style.name.startswith("Heading")
    ]


def test_docx_references_before_acronyms(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    heads = _back_matter_headings(draft, hub, tmp_path)
    assert heads.index("References") < heads.index("Acronyms")


def test_docx_back_matter_follows_shared_constant(
    draft: DraftHandler, hub: Hub, tmp_path: Path, monkeypatch
) -> None:
    from precis.export import _back_matter

    monkeypatch.setattr(
        _back_matter,
        "BACK_MATTER_ORDER",
        (
            "acronyms",
            "references",
            "unverified",
            "data_package",
            "published",
            "sources",
        ),
    )
    heads = _back_matter_headings(draft, hub, tmp_path)
    assert heads.index("Acronyms") < heads.index("References")


# ── gr725240: superscript minus ────────────────────────────────────────


def test_script_minus_is_u2212_before_a_digit_or_alone() -> None:
    """A script-only math span ``$^{-1}$`` and authored ``<sup>-1</sup>``
    print U+2212; a hyphen inside a word-like subscript stays a hyphen."""
    from precis.export import docx as dx

    ctx = _abbrev_ctx({})
    para = docx.Document().add_paragraph()
    dx._render_inline(
        "cm$^{-1}$, mAh g$^{-1}$, <sup>-2</sup>, x$^{-}$, y$^{+-}$,"
        " k$_{in-plane}$, z$^{2-}$, w$_{-1}$.",
        ctx,
        para,
    )
    sups = [r.text for r in para.runs if r.font.superscript]
    subs = [r.text for r in para.runs if r.font.subscript]
    assert sups == ["\u22121", "\u22121", "\u22122", "\u2212", "+\u2212", "2\u2212"]
    assert subs == ["in-plane", "\u22121"]
    assert not para._p.xpath(".//m:oMath")


def test_script_charge_minus_is_u2212_in_superscripts_only() -> None:
    """A superscript that ends in a hyphen after a digit is a charge (``2-``);
    a word-like superscript and a subscript keep the hyphen."""
    from precis.export import docx as dx

    ctx = _abbrev_ctx({})
    para = docx.Document().add_paragraph()
    dx._render_inline(
        "SO$^{2-}$, <sup>3-</sup>, a<sup>x-ray</sup>, b<sup>e-</sup>, c<sub>2-</sub>.",
        ctx,
        para,
    )
    sups = [r.text for r in para.runs if r.font.superscript]
    subs = [r.text for r in para.runs if r.font.subscript]
    assert sups == ["2\u2212", "3\u2212", "x-ray", "e-"]
    assert subs == ["2-"]


# ── gr725247: document properties ────────────────────────────────────


def test_docx_core_properties_come_from_the_draft(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    from datetime import UTC, datetime

    pid = _new_draft_project(hub)
    draft.put(id="props", title="Films of C$_{60}$ on <i>Au</i>", project=pid)
    draft.edit(id="props", authors=[{"name": "Doe, Jane"}, {"name": "Roe, John"}])
    ref = hub.live_store.get_ref(kind="draft", id="props")
    out = tmp_path / "props.docx"
    export_docx(hub.live_store, ref, target_path=out)
    cp = docx.Document(str(out)).core_properties
    assert cp.title == "Films of C60 on Au"
    assert cp.author == "Jane Doe, John Roe"
    assert cp.comments == ""
    assert cp.last_modified_by == ""
    assert cp.created is not None and cp.created.year >= datetime.now(UTC).year
    assert cp.modified is not None and cp.modified.year >= datetime.now(UTC).year

    # no byline: the author stays empty, never the template's "python-docx"
    draft.put(id="props2", title="Plain", project=_new_draft_project(hub))
    ref2 = hub.live_store.get_ref(kind="draft", id="props2")
    out2 = tmp_path / "props2.docx"
    export_docx(hub.live_store, ref2, target_path=out2)
    cp2 = docx.Document(str(out2)).core_properties
    assert cp2.author == "" and cp2.title == "Plain"
    assert "python-docx" not in cp2._element.xml


# ── gr725249: header rows, alt text, resolution ───────────────────────


def test_back_matter_tables_repeat_their_header_row() -> None:
    from precis.export import docx as dx

    doc = docx.Document()
    dx._append_supplement(
        doc, [SimpleNamespace(number="S1", kind="figure", description="d")]
    )
    (table,) = doc.tables
    assert [bool(r._tr.xpath("w:trPr/w:tblHeader")) for r in table.rows] == [
        True,
        False,
    ]


def test_alt_text_strips_marks_and_math_and_cuts_on_a_word() -> None:
    from precis.export import docx as dx

    assert (
        dx._alt_text("Spectra of $\\mathrm{CO_2}$ [pc123] at **low** T.")
        == "Spectra of CO2 at low T."
    )
    long = "word " * 80
    alt = dx._alt_text(long)
    assert len(alt) <= 250 and alt.endswith("word") and not alt.endswith(" ")
    assert dx._alt_text("") == ""


def _png_of_width(width: int) -> str:
    import base64
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, 10), "white").save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_raster_figure_alt_text_and_low_resolution_warning(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """1000 px over 5.5 in is 181.8 dpi: warned, rounded down so a value
    just under the limit never prints as the limit. 1800 px is 327 dpi:
    silent. Each picture carries its caption as ``descr``."""
    pytest.importorskip("PIL")
    pid = _new_draft_project(hub)
    draft.put(id="dpi", title="T", project=pid)
    ref = hub.live_store.get_ref(kind="draft", id="dpi")
    assert ref is not None
    for text, width in (("Low resolution.", 1000), ("High resolution.", 1800)):
        draft.put(
            id="dpi",
            chunk_kind="figure",
            text=text,
            image=_png_of_width(width),
            origin="original",
            at={"last": True},
        )
    figs = [
        c
        for c in hub.live_store.drafts.reading_order(ref.id)
        if c.chunk_kind == "figure"
    ]
    out = tmp_path / "dpi.docx"
    res = export_docx(hub.live_store, ref, target_path=out)
    assert [w for w in res.warnings if "dpi" in w] == [
        f"figure {figs[0].dc}: 181 dpi at 5.5 in (1000 px wide); below 300 dpi"
    ]
    shapes = docx.Document(str(out)).inline_shapes
    assert [s._inline.docPr.get("descr") for s in shapes] == [
        "Low resolution.",
        "High resolution.",
    ]


# ── gr725242: thin space in math ───────────────────────────────────────


def test_latex_spacing_macros_survive_in_omml() -> None:
    """``$6\\,\\mu_B$`` keeps a thin space between number and unit; ``\\;`` and
    ``\\:`` give a medium space, ``\\ `` a no-break space, ``\\!`` stays dropped."""
    from precis.export import docx as dx

    ctx = _abbrev_ctx({})
    for src, text in (
        (r"$6\,\mu_B$", "6\u2009\u03bcB"),
        (r"$5.76\,\mu_B$", "5.76\u2009\u03bcB"),
        (r"$a\;b\:c$", "a\u205fb\u205fc"),
        (r"$a\ b$", "a\u00a0b"),
        (r"$a\!b$", "ab"),
    ):
        para = docx.Document().add_paragraph()
        dx._render_inline(src, ctx, para)
        omath = para._p.xpath(".//m:oMath")
        assert len(omath) == 1, src
        ns = {"m": "http://schemas.openxmlformats.org/officeDocument/2006/math"}
        assert "".join(omath[0].xpath(".//m:t/text()", namespaces=ns)) == text, src


# ── gr725241: upright operators and unit symbols ───────────────────────

_M_NS = {"m": "http://schemas.openxmlformats.org/officeDocument/2006/math"}


def test_named_operators_render_upright_in_omml() -> None:
    """``\\sin`` (mi) and ``\\max``/``\\lim`` (mo) get ``m:sty p``; the
    variables around them stay default (italic) math."""
    from precis.export.omml import latex_to_omml

    omath = latex_to_omml(
        r"\sin(\theta/2) = 1 - P/6 + \cos x + \exp y + \log z + \ln w"
        r" + \tan a + \min b + \max c + \det d + \lim_{x \to 0} e"
    )
    assert omath is not None
    upright = {}
    for r in omath.xpath(".//m:r", namespaces=_M_NS):
        t = r.xpath("string(m:t)", namespaces=_M_NS)
        upright[t] = r.xpath("m:rPr/m:sty/@m:val", namespaces=_M_NS) == ["p"]
    for name in ("sin", "cos", "exp", "log", "ln", "tan", "min", "max", "det", "lim"):
        assert upright[name], name
    for var in ("x", "P", "\u03b8", "e"):
        assert not upright[var], var


def test_unit_mu_and_omega_become_plain_characters() -> None:
    from precis.export import docx as dx

    ctx = _abbrev_ctx({})
    para = docx.Document().add_paragraph()
    dx._render_inline(
        r"V/$\mu$m, 10 $\mu$A/cm$^2$, 150 $\Omega$, 5$\Omega$, k$\Omega$,"
        r" M$\Omega$, 4.7k$\Omega$, (m$\Omega$) and 3"
        "\u00a0"
        r"$\Omega$",
        ctx,
        para,
    )
    assert para.text == (
        "V/\u03bcm, 10 \u03bcA/cm2, 150 \u03a9, 5\u03a9, k\u03a9, M\u03a9,"
        " 4.7k\u03a9, (m\u03a9) and 3\u00a0\u03a9"
    )
    assert not para._p.xpath(".//m:oMath")
    # plain runs: not italic, not math
    assert not any(r.italic for r in para.runs)


def test_other_mu_and_omega_spans_stay_math() -> None:
    from precis.export import docx as dx

    ctx = _abbrev_ctx({})
    for src in (
        r"$\mu_B$",  # a subscripted mu
        r"a lone $\mu$ here",  # mu before a space
        r"in $\Omega$ space",  # Omega after a word and a space
        r"$\Omega$ at the start",  # Omega with nothing before it
        r"7  $\Omega$",  # two spaces: not a unit
        r"$2\mu$m",  # not exactly \mu
        r"system$\Omega$",  # the prefix letter ends a word
        r"(a) system$\Omega$",
    ):
        para = docx.Document().add_paragraph()
        dx._render_inline(src, ctx, para)
        assert para._p.xpath(".//m:oMath"), src


# ── repeated citations in one block (gr725253) ─────────────────────────


def _repeat_ctx(**kw: Any):
    from precis.export import docx as dx

    return dx._Ctx(
        store=_FindingFake({}),
        known_handles=set(),
        doi_links=False,
        library_links=False,
        library_label="x",
        library_search_url="x",
        **kw,
    )


def test_repeated_cite_in_a_paragraph_prints_once() -> None:
    ctx = _repeat_ctx()
    text = _render_text(
        "Alpha [§a1] one. Beta [§a1]. Gamma [§b2] then [§a1] end [§a1].", ctx
    )
    # first mark stays; an equal one after prose goes; a different mark in
    # between resets the comparison
    assert text == "Alpha [1] one. Beta. Gamma [2] then [1] end."
    assert ctx.cited == ["a1", "b2"]  # a dropped mark still counts as cited


def test_dropped_cite_closes_up_the_prose() -> None:
    ctx = _repeat_ctx()
    assert (
        _render_text("as shown [§a1], and then [§a1], and so on [§a1] and [§a1]", ctx)
        == "as shown [1], and then, and so on and [1]"  # "and [1]" leans
    )
    ctx = _repeat_ctx()
    assert _render_text("text [§a1]. More [§a1]. Then [§a1] more.", ctx) == (
        "text [1]. More. Then more."
    )


#: ``(source after a first printed mark, output with the mark as {c})``: the
#: sentence leans on a repeated mark and keeps it, or it is dropped and the
#: prose closes up. The Word and LaTeX tests below render the same table.
_LEANING_REPEATS = [
    ("B (see [§a1]).", "B (see {c})."),
    ("B ([§a1]).", "B ({c})."),
    ("as in ref. [§a1].", "as in ref. {c}."),
    ("(Fig. 2, [§a1])", "(Fig. 2, {c})"),
    ("the model [§a1]'s core", "the model {c}'s core"),
    ("It ended. [§a1] shows that", "It ended. {c} shows that"),
    ("text [§a1]. More", "text. More"),
    ("text [§a1], and", "text, and"),
    ("text [§a1] more", "text more"),
    ("(text [§a1])", "(text)"),
]


@pytest.mark.parametrize(("source", "expected"), _LEANING_REPEATS)
def test_a_repeat_the_sentence_leans_on_is_kept(source: str, expected: str) -> None:
    text = _render_text("First [§a1] here. " + source, _repeat_ctx())
    assert text == "First [1] here. " + expected.format(c="[1]")


def test_a_kept_repeat_is_what_the_next_repeat_is_compared_with() -> None:
    ctx = _repeat_ctx()
    assert _render_text("A [§a1]. B (see [§a1]) C [§a1] D [§b2] E [§a1].", ctx) == (
        "A [1]. B (see [1]) C D [2] E [1]."
    )


def test_each_block_starts_its_own_comparison() -> None:
    ctx = _repeat_ctx()
    assert _render_text("one [§a1].", ctx) == "one [1]."
    assert _render_text("two [§a1].", ctx) == "two [1]."


def test_repeat_cites_true_prints_every_mark() -> None:
    ctx = _repeat_ctx(repeat_cites=True)
    text = "Beta [§a1]. Gamma [§a1] then [§a1]."
    assert _render_text(text, ctx) == "Beta [1]. Gamma [1] then [1]."


def test_si_mark_differs_from_the_plain_reference(monkeypatch) -> None:
    ctx = _si_docx_ctx(monkeypatch, with_parent=True)
    out = _render_text("A [§parent24] x [§parent24si] y [§parent24si] z.", ctx)
    assert out == "A [1] x [1] (SI) y z."


def test_trust_marked_cite_is_never_dropped(monkeypatch) -> None:
    from types import SimpleNamespace

    from precis.export import docx as dx

    monkeypatch.setattr(dx, "_finding_cite_keys_pinned", lambda *_a: ["a1"])
    ctx = _repeat_ctx()
    labels = {7: "unverified", 8: "clean"}
    ctx.trust = SimpleNamespace(
        resolve=lambda pk: SimpleNamespace(label=labels[pk], note="")
    )
    out = _render_text("A [fi7] x [fi7] y [fi8] z [fi8] w [§a1] v.", ctx)
    # both trust-marked cites print, then the comparison starts over: the
    # clean cite prints once, its repeat and the plain [§a1] after it go
    assert out == "A [1] [unverified] x [1] [unverified] y [1] z w v."


def _passage_ctx(monkeypatch):
    from types import SimpleNamespace

    from precis.export import docx as dx

    passages = {10: "First passage.", 11: "Second passage."}
    store = SimpleNamespace(
        get_ref=lambda **_k: None,
        resolve_handle=lambda h: SimpleNamespace(
            public_id="a1", chunk_id=int(h[2:]), cite_public_id=None
        ),
        chunks=SimpleNamespace(chunk_text_by_id=lambda cid: passages[cid]),
    )
    monkeypatch.setattr(
        dx,
        "_resolve_source",
        lambda _s, slug: {
            "rec_number": 1,
            "title": slug,
            "authors": ["Doe, J."],
            "year": 2024,
            "slug": slug,
        },
    )
    return dx._Ctx(
        store=store,
        known_handles=set(),
        endnote=True,
        doi_links=False,
        library_links=False,
        library_label="x",
        library_search_url="x",
    )


def _two_record_passage_ctx(monkeypatch):
    from types import SimpleNamespace

    from precis.export import docx as dx

    passages = {
        10: "First passage.",
        11: "Second passage.",
        12: "First",  # a substring of the first, still a note of its own
        20: "Third passage.",
        21: "Fourth passage.",
    }
    store = SimpleNamespace(
        get_ref=lambda **_k: None,
        resolve_handle=lambda h: SimpleNamespace(
            public_id="a1" if int(h[2:]) < 20 else "b2",
            chunk_id=int(h[2:]),
            cite_public_id=None,
        ),
        chunks=SimpleNamespace(chunk_text_by_id=lambda cid: passages[cid]),
    )
    monkeypatch.setattr(
        dx,
        "_resolve_source",
        lambda _s, slug: {
            "rec_number": 1 if slug == "a1" else 2,
            "title": slug,
            "authors": ["Doe, J."],
            "year": 2024,
            "slug": slug,
        },
    )
    return dx._Ctx(
        store=store,
        known_handles=set(),
        endnote=True,
        doi_links=False,
        library_links=False,
        library_label="x",
        library_search_url="x",
    )


def test_endnote_dropped_pair_adds_a_note_to_each_record(monkeypatch) -> None:
    from lxml import etree

    from precis.export import docx as dx

    ctx = _two_record_passage_ctx(monkeypatch)
    para = docx.Document().add_paragraph()
    dx._render_inline(
        "A [pc10][pc20] then [pc11][pc21] more [pc12][pc20] end.", ctx, para
    )
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    instrs = [e.text or "" for e in para._p.iter(w + "instrText")]
    assert len(instrs) == 1  # the repeated pair prints no field
    assert para.text == "A [1, 2] then more end."  # the kept field's display text
    payload = instrs[0].strip()[len("ADDIN EN.CITE ") :]
    first, second = etree.fromstring(payload.encode("utf-8"))
    notes = [c.findtext("record/research-notes") or "" for c in (first, second)]
    # each record gets its own passages; the pinned "First" is a note of its
    # own although "First passage." contains it; "Third passage." only once
    assert [n.split() for n in notes] == [
        ["First", "passage.", "Second", "passage.", "First"],
        ["Third", "passage.", "Fourth", "passage."],
    ]
    assert notes[0].count("First passage.") == 1 and notes[0].endswith("First")
    assert notes[1].count("Third passage.") == 1


def test_endnote_dropped_mark_adds_its_passage_to_the_kept_record(monkeypatch) -> None:
    from lxml import etree

    from precis.export import docx as dx

    ctx = _passage_ctx(monkeypatch)
    para = docx.Document().add_paragraph()
    dx._render_inline("First [pc10] then more [pc11] again [pc10].", ctx, para)
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    instrs = [e.text or "" for e in para._p.iter(w + "instrText")]
    assert len(instrs) == 1  # no field for the dropped marks
    assert para.text == "First [1] then more again."
    payload = instrs[0].strip()[len("ADDIN EN.CITE ") :]
    (cite,) = etree.fromstring(payload.encode("utf-8"))
    notes = cite.findtext("record/research-notes")
    # each pinned passage kept once, none lost
    assert notes == "First passage. Second passage."


def test_endnote_records_carry_no_notes_when_research_notes_off(monkeypatch) -> None:
    """``research_notes=False``: the kept field's records have no
    ``<research-notes>`` element, neither from their own cite nor through the
    merged-notes path of a dropped repeat; the cites still print as before."""
    from lxml import etree

    from precis.export import docx as dx

    ctx = _passage_ctx(monkeypatch)
    ctx.research_notes = False
    para = docx.Document().add_paragraph()
    dx._render_inline("First [pc10] then more [pc11] again [pc10].", ctx, para)
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    instrs = [e.text or "" for e in para._p.iter(w + "instrText")]
    assert len(instrs) == 1
    assert para.text == "First [1] then more again."
    payload = instrs[0].strip()[len("ADDIN EN.CITE ") :]
    (cite,) = etree.fromstring(payload.encode("utf-8"))
    assert cite.find("record") is not None
    assert cite.find("record/research-notes") is None
    assert "research-notes" not in payload
    assert "passage" not in payload


def test_export_docx_research_notes_switch(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """End to end: a ``[pc<id>]`` cite embeds its passage by default and
    ``research_notes=False`` drops it; plain mode never had it either way."""
    import zipfile

    store = hub.live_store
    store.insert_ref(
        kind="paper", slug="nn07", title="Notes paper", year=2007, provider="manual"
    )
    pref = store.get_ref(kind="paper", id="nn07")
    assert pref is not None
    store.chunks.insert_chunks(
        pref.id, [ChunkInsert(ord=0, text="Passage about junction sp3.", slug="b0")]
    )
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "ORDER BY ord LIMIT 1",
            (pref.id,),
        ).fetchone()
        assert row is not None
        chunk_id = int(row[0])
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="nsw", title="T", project=pid)
    draft.put(
        id="nsw",
        chunk_kind="paragraph",
        text=f"Junctions [pc{chunk_id}] again [pc{chunk_id}].",
        at={"last": True},
    )
    ref = store.get_ref(kind="draft", id="nsw")

    def body_of(name: str, **kw: Any) -> str:
        out = tmp_path / f"{name}.docx"
        export_docx(store, ref, target_path=out, **kw)
        return zipfile.ZipFile(out).read("word/document.xml").decode("utf-8")

    on = body_of("on", citations="endnote")
    default = body_of("default", citations="endnote", research_notes=True)
    off = body_of("off", citations="endnote", research_notes=False)
    plain = body_of("plain", citations="plain", research_notes=False)
    assert "&lt;research-notes&gt;" in on and "junction sp3" in on
    assert on == default
    assert " ADDIN EN.CITE " in off
    assert "research-notes" not in off and "junction sp3" not in off
    assert "ADDIN EN.CITE" not in plain


@pytest.mark.parametrize("mode", ["word", "zotero"])
def test_export_docx_named_but_unbuilt_citation_modes_refuse(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every mode is built now; a named mode missing from the constant still
    refuses before anything is written."""
    import precis.export as export
    from precis.errors import BadInput

    monkeypatch.setattr(export, "IMPLEMENTED_CITATION_MODES", ("endnote", "plain"))
    none: Any = None  # the mode is refused before the store or the ref is read
    with pytest.raises(BadInput, match="not available yet"):
        export_docx(none, none, target_path=tmp_path / "x.docx", citations=mode)
    assert not (tmp_path / "x.docx").exists()


@pytest.mark.parametrize("mode", ["EndNote", "", "bibtex"])
def test_export_docx_unknown_citation_mode_refuses(mode: str, tmp_path: Path) -> None:
    from precis.errors import BadInput

    none: Any = None  # the mode is refused before the store or the ref is read
    with pytest.raises(BadInput, match="unknown citations mode"):
        export_docx(none, none, target_path=tmp_path / "x.docx", citations=mode)


def test_repeated_cite_dropped_in_caption_and_cell() -> None:
    from precis.export import docx as dx

    ctx = _repeat_ctx()
    doc = docx.Document()
    cap, cell = doc.add_paragraph(), doc.add_table(rows=1, cols=1).cell(0, 0)
    dx._render_inline("Cap [§a1] plus [§a1] end.", ctx, cap)
    dx._render_inline("Cell [§a1] plus [§a1].", ctx, cell.paragraphs[0])
    assert cap.text == "Cap [1] plus end."
    assert cell.text == "Cell [1] plus."


# ── one number for two records of the same work (gr725253) ─────────────


def _doi_store():
    from tests.test_export_walker import _SMITH, _WorkStore

    return _WorkStore(
        {
            "a1": ("Nanobuds", 2020, _SMITH, "10.1/X"),
            "b2": ("Nanobuds, preprint", 2019, _SMITH, "https://doi.org/10.1/x"),
            "c3": ("Other", 2021, _SMITH, "10.9/y"),
        }
    )


def test_two_records_with_one_doi_share_a_number_in_docx() -> None:
    from precis.export import docx as dx

    ctx = dx._Ctx(
        store=_doi_store(),
        known_handles=set(),
        doi_links=False,
        library_links=False,
        library_label="x",
        library_search_url="x",
    )
    text = _render_text("A [§a1] x [§c3] y [§b2] z.", ctx)
    assert text == "A [1] x [2] y [1] z."
    assert ctx.cited == ["a1", "c3"]  # no list entry for b2
    assert ctx.warnings == [
        "references a1 and b2 share DOI 10.1/x: printed as one entry"
    ]


def test_same_doi_records_inside_one_claim_hub_share_a_number_in_docx(
    monkeypatch,
) -> None:
    import types

    from precis.export import docx as dx

    store = _doi_store()
    store.pool = _NonHubPool()
    store.fetch_refs_by_ids = lambda ids: {
        i: types.SimpleNamespace(meta={}) for i in ids
    }
    store.tags_for = lambda _id: []
    monkeypatch.setattr(dx, "_conjunct_cite_keys", lambda _s, _pk: ["a1", "b2"])
    monkeypatch.setattr(dx, "_render_trust_mark", lambda *_a: None)
    ctx = dx._Ctx(
        store=store,
        known_handles=set(),
        doi_links=False,
        library_links=False,
        library_label="x",
        library_search_url="x",
    )
    assert _render_text("claim [fi7].", ctx) == "claim [1]."
    assert ctx.cited == ["a1"]
    assert ctx.warnings == [
        "references a1 and b2 share DOI 10.1/x: printed as one entry"
    ]


def test_export_warns_when_two_cited_records_look_like_one_work(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    _seed_paper(hub.live_store, "dupa2020", "A study of MOFs", 2020)
    _seed_paper(hub.live_store, "dupb2020", "A study  of MOFs.", 2020)
    pid = _new_draft_project(hub)
    draft.put(id="dsame", title="T", project=pid)
    draft.put(
        id="dsame",
        chunk_kind="paragraph",
        text="One [§dupa2020] and the other [§dupb2020].",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="dsame")
    res = export_docx(hub.live_store, ref, target_path=tmp_path / "dsame.docx")
    assert res.cited_slugs == ["dupa2020", "dupb2020"]  # no DOI: not merged
    assert (
        "references [1] and [2] look like the same work (dupa2020, dupb2020)"
        in res.warnings
    )

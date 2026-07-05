"""Draft → .docx export — structure, inline formatting, citation
integrity (numbered references resolved through the shared paper lookup),
and the glossary. Round-trips through python-docx, which is itself a
validity check (a corrupt part would fail to re-open)."""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler
from precis.handlers.todo import TodoHandler
from precis.store import BlockInsert, Store

docx = pytest.importorskip("docx")  # python-docx (the `docx` extra)

from precis.export.docx import export_docx


def _seed_paper(store: Store, slug: str, title: str, year: int) -> None:
    store.insert_ref(kind="paper", slug=slug, title=title, year=year, provider="manual")
    store.insert_blocks(
        store.get_ref(kind="paper", id=slug).id,
        [BlockInsert(pos=0, text="body", slug="b0")],
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
    return hub.store.get_ref(kind="draft", id="d1")


def test_export_produces_valid_docx(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    _seed_paper(hub.store, "miller2020", "A study of MOFs", 2020)
    ref = _make_draft(draft, hub)
    out = tmp_path / "d1.docx"
    res = export_docx(hub.store, ref, target_path=out)
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
    _seed_paper(hub.store, "miller2020", "A study of MOFs", 2020)
    ref = _make_draft(draft, hub)
    out = tmp_path / "d1.docx"
    res = export_docx(hub.store, ref, target_path=out)
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
    _seed_paper(hub.store, "wu22", "Wu 2022 study", 2022)
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
    ref = hub.store.get_ref(kind="draft", id="dr")
    out = tmp_path / "dr.docx"
    res = export_docx(hub.store, ref, target_path=out)
    assert res.cited_slugs == ["wu22"]
    import zipfile

    with zipfile.ZipFile(out) as z:
        body = z.read("word/document.xml").decode("utf-8")
    # Two non-adjacent marks → the same number reused, no endnote machinery.
    assert body.count("endnoteReference") == 0
    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    assert text.count("[1]") >= 2  # the mark repeats (≥2 sites + References)
    assert "Wu 2022 study" in text  # one resolved entry


def test_glossary_section(draft: DraftHandler, hub: Hub, tmp_path: Path) -> None:
    _seed_paper(hub.store, "miller2020", "A study of MOFs", 2020)
    ref = _make_draft(draft, hub)
    out = tmp_path / "d1.docx"
    export_docx(hub.store, ref, target_path=out)
    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    assert "Glossary" in text
    assert "MOF" in text and "metal-organic framework" in text
    # The draft's own Glossary is the abbreviations list — the auto "Acronyms"
    # section would duplicate it, so it is suppressed (one section, not two).
    assert "Acronyms" not in text


def test_missing_paper_warns_but_exports(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    ref = _make_draft(draft, hub)  # cites miller2020 which is NOT seeded
    out = tmp_path / "d1.docx"
    res = export_docx(hub.store, ref, target_path=out)
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
    ref = hub.store.get_ref(kind="draft", id="dx")
    out = tmp_path / "dx.docx"
    export_docx(hub.store, ref, target_path=out)
    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    # First prose occurrence expanded; later plural stays abbreviated.
    assert "metal-organic framework (MOF)" in text
    assert "MOFs appear" in text  # plural, not expanded
    # The abbreviation is an explicit term, so it lives in the Glossary; the
    # auto "Acronyms" section is suppressed to avoid a duplicate list.
    assert "Glossary" in text
    assert "Acronyms" not in text


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
    ref = hub.store.get_ref(kind="draft", id="dm")
    out = tmp_path / "dm.docx"
    export_docx(hub.store, ref, target_path=out)
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
    ref = hub.store.get_ref(kind="draft", id="dz")
    out = tmp_path / "dz.docx"
    export_docx(hub.store, ref, target_path=out)
    import zipfile

    with zipfile.ZipFile(out) as z:
        doc_xml = z.read("word/document.xml").decode("utf-8")
    assert "<m:e/>" not in doc_xml  # no empty base → no dotted box
    assert "oMath" in doc_xml
    docx.Document(str(out))


def test_latex_cite_command_is_folded(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """A draft carrying verbatim LaTeX ``\\cite{key}`` resolves to a numbered
    mark + References entry — the ``\\cite{`` / ``}`` wrapper never leaks as
    literal text."""
    _seed_paper(hub.store, "wu22", "Wu 2022 study", 2022)
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
    ref = hub.store.get_ref(kind="draft", id="dc")
    out = tmp_path / "dc.docx"
    res = export_docx(hub.store, ref, target_path=out)
    assert res.cited_slugs == ["wu22"]
    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    assert "\\cite" not in text and "cite{" not in text  # wrapper folded away
    assert "[1]" in text  # resolved to a numbered mark
    assert "Wu 2022 study" in text  # References entry


def test_handle_form_citation_resolves(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """A draft that cites a paper by ADR-0036 handle (``[pa<ref_id>]``, the
    form every LaTeX-imported draft uses) must produce a numbered mark +
    References entry — NOT render as nothing. Regression: the docx exporter
    used to drop handle citations entirely (the LaTeX/PDF path resolved them),
    so handle-cited drafts exported with no citations and no References
    section at all."""
    from precis.utils import handle_registry

    _seed_paper(hub.store, "nasibulin2007", "Multifunctional nanobuds", 2007)
    pref = hub.store.get_ref(kind="paper", id="nasibulin2007")
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
    ref = hub.store.get_ref(kind="draft", id="dh")
    out = tmp_path / "dh.docx"
    res = export_docx(hub.store, ref, target_path=out)
    assert res.cited_slugs == ["nasibulin2007"]  # handle resolved to the slug
    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    assert "[1]" in text  # numbered mark emitted (not dropped)
    assert "References" in text
    assert "Multifunctional nanobuds" in text  # resolved entry


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
    _seed_paper(hub.store, "wu22", "Wu 2022 study", 2022)
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
    ref = hub.store.get_ref(kind="draft", id="dd")
    out = tmp_path / "dd.docx"
    res = export_docx(hub.store, ref, target_path=out)
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

    ref = hub.store.get_ref(kind="draft", id="ld")
    out = tmp_path / "ld.docx"
    export_docx(hub.store, ref, target_path=out)
    doc = docx.Document(str(out))
    styled = {
        p.text: p.style.name for p in doc.paragraphs if p.text in ("alpha", "one")
    }
    assert styled.get("alpha") == "List Bullet"
    assert styled.get("one") == "List Number"


def test_export_renders_table(draft: DraftHandler, hub: Hub, tmp_path: Path) -> None:
    """A chunk_kind='table' becomes a native Word table (ADR 0035 §1):
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
    ref = hub.store.get_ref(kind="draft", id="tb")
    out = tmp_path / "tb.docx"
    export_docx(hub.store, ref, target_path=out)

    doc = docx.Document(str(out))
    assert len(doc.tables) == 1
    t = doc.tables[0]
    assert len(t.rows) == 3 and len(t.columns) == 2  # header + 2 body rows
    assert [c.text for c in t.rows[0].cells] == ["ID", "Title"]
    assert [c.text for c in t.rows[1].cells] == ["I1", "alpha"]
    assert t.rows[0].cells[0].paragraphs[0].runs[0].bold  # header bold
    # caption rendered as a bold lead-in paragraph; pipe markdown not dumped
    paras = [p.text for p in doc.paragraphs]
    assert "Issue register" in paras
    assert not any("| ID | Title |" in p for p in paras)


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
    assert "Doe, Jane" in text and "Roe, John" in text
    # superscript marks present as run text on the names paragraph
    names_p = doc.paragraphs[1]
    assert any(r.font.superscript and r.text in ("1", "2") for r in names_p.runs)
    # the ROR affiliation is a real external hyperlink relationship
    xml = doc.paragraphs[2]._p.xml
    assert "hyperlink" in xml
    rels = doc.part.rels
    assert any(r.reltype.endswith("hyperlink") for r in rels.values())


def test_render_byline_empty_authors_is_noop() -> None:
    from precis.export.docx import _render_byline
    from precis.utils.authors import build_byline

    doc = docx.Document()
    doc.add_heading("Title", level=0)
    before = len(doc.paragraphs)
    _render_byline(doc, build_byline(None))
    assert len(doc.paragraphs) == before

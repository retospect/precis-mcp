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
        TodoHandler(hub=hub).put(text="proj").body.split("id=")[1].split()[0].rstrip(",.()")
    )
    draft.put(id="d1", title="CO2 Capture in MOFs", project=pid)
    sec = draft.put(id="d1", chunk_kind="heading", text="Methods", at={"last": True})
    sec_h = sec.body.split("¶")[1].split()[0]
    draft.put(
        id="d1", chunk_kind="paragraph",
        text="Amine **functionalization** improves uptake [§miller2020~0].",
        at={"into": f"¶{sec_h}", "last": True},
    )
    draft.put(id="d1", chunk_kind="term", text="metal-organic framework",
              meta={"short": "MOF"})
    return hub.store.get_ref(kind="draft", id="d1")


def test_export_produces_valid_docx(draft: DraftHandler, hub: Hub, tmp_path: Path) -> None:
    _seed_paper(hub.store, "miller2020", "A study of MOFs", 2020)
    ref = _make_draft(draft, hub)
    out = tmp_path / "d1.docx"
    res = export_docx(hub.store, ref, target_path=out)
    assert out.is_file()
    # Re-open (validity check) and read the text.
    doc = docx.Document(str(out))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "CO2 Capture in MOFs" in text   # title
    assert "Methods" in text               # heading
    assert "functionalization" in text     # bold run content
    # Citation is a real Word endnote reference in the body, not "[1]" text.
    import zipfile
    with zipfile.ZipFile(out) as z:
        assert "endnoteReference" in z.read("word/document.xml").decode("utf-8")


def test_citation_integrity_in_endnotes(draft: DraftHandler, hub: Hub, tmp_path: Path) -> None:
    _seed_paper(hub.store, "miller2020", "A study of MOFs", 2020)
    ref = _make_draft(draft, hub)
    out = tmp_path / "d1.docx"
    res = export_docx(hub.store, ref, target_path=out)
    assert res.cited_slugs == ["miller2020"]
    docx.Document(str(out))  # round-trip validity check
    import zipfile
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        assert "word/endnotes.xml" in names           # the endnotes part exists
        endnotes = z.read("word/endnotes.xml").decode("utf-8")
        ct = z.read("[Content_Types].xml").decode("utf-8")
        rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
    assert "A study of MOFs" in endnotes               # resolved title (integrity)
    assert "2020" in endnotes
    assert 'w:id="1"' in endnotes                      # content endnote id 1
    assert "endnotes+xml" in ct                        # content-type override
    assert "relationships/endnotes" in rels            # wired to the document


def test_glossary_section(draft: DraftHandler, hub: Hub, tmp_path: Path) -> None:
    _seed_paper(hub.store, "miller2020", "A study of MOFs", 2020)
    ref = _make_draft(draft, hub)
    out = tmp_path / "d1.docx"
    export_docx(hub.store, ref, target_path=out)
    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    assert "Glossary" in text
    assert "MOF" in text and "metal-organic framework" in text


def test_missing_paper_warns_but_exports(draft: DraftHandler, hub: Hub, tmp_path: Path) -> None:
    ref = _make_draft(draft, hub)  # cites miller2020 which is NOT seeded
    out = tmp_path / "d1.docx"
    res = export_docx(hub.store, ref, target_path=out)
    assert out.is_file()
    assert any("miller2020" in w for w in res.warnings)


def test_acronym_first_use_expansion(draft: DraftHandler, hub: Hub, tmp_path: Path) -> None:
    pid = int(
        TodoHandler(hub=hub).put(text="proj").body.split("id=")[1].split()[0].rstrip(",.()")
    )
    draft.put(id="dx", title="T", project=pid)
    draft.put(id="dx", chunk_kind="term", text="metal-organic framework",
              meta={"short": "MOF"})
    draft.put(id="dx", chunk_kind="paragraph",
              text="First, MOF systems. Later, more MOFs appear.", at={"last": True})
    ref = hub.store.get_ref(kind="draft", id="dx")
    out = tmp_path / "dx.docx"
    export_docx(hub.store, ref, target_path=out)
    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    # First prose occurrence expanded; later plural stays abbreviated.
    assert "metal-organic framework (MOF)" in text
    assert "MOFs appear" in text          # plural, not expanded
    assert "Acronyms" in text             # auto-built acronym list


def test_math_renders_as_omml(draft: DraftHandler, hub: Hub, tmp_path: Path) -> None:
    pytest.importorskip("latex2mathml")
    pid = int(
        TodoHandler(hub=hub).put(text="proj").body.split("id=")[1].split()[0].rstrip(",.()")
    )
    draft.put(id="dm", title="T", project=pid)
    draft.put(id="dm", chunk_kind="paragraph",
              text="The relation $E = mc^2$ and a fraction $\\frac{a}{b}$.",
              at={"last": True})
    ref = hub.store.get_ref(kind="draft", id="dm")
    out = tmp_path / "dm.docx"
    export_docx(hub.store, ref, target_path=out)
    # Inspect the document XML for native OMML math.
    import zipfile
    with zipfile.ZipFile(out) as z:
        doc_xml = z.read("word/document.xml").decode("utf-8")
    assert "oMath" in doc_xml          # native math, not literal "$...$"
    assert "sSup" in doc_xml           # the c^2 superscript
    assert "}f" in doc_xml or "<m:f" in doc_xml or ":f>" in doc_xml  # the fraction
    assert "$" not in doc_xml          # no leftover literal math source
    # Re-open as a validity check.
    docx.Document(str(out))


def test_omml_converter_returns_none_on_empty() -> None:
    pytest.importorskip("latex2mathml")
    from precis.export.omml import latex_to_omml

    assert latex_to_omml("") is None
    assert latex_to_omml("   ") is None
    el = latex_to_omml("x^2")  # well-formed → an <m:oMath> element
    assert el is not None and el.tag.endswith("oMath")


def test_same_paper_chunks_collapse_to_one_endnote(draft: DraftHandler, hub: Hub, tmp_path: Path) -> None:
    """a~3, a~9, a~23 (different chunks, same paper) → ONE endnote, and
    consecutive marks collapse to a single reference."""
    _seed_paper(hub.store, "wu22", "Wu 2022 study", 2022)
    pid = int(
        TodoHandler(hub=hub).put(text="proj").body.split("id=")[1].split()[0].rstrip(",.()")
    )
    draft.put(id="dd", title="T", project=pid)
    draft.put(id="dd", chunk_kind="paragraph",
              text="Several findings [§wu22~3] [§wu22~9] [§wu22~23] agree.",
              at={"last": True})
    ref = hub.store.get_ref(kind="draft", id="dd")
    out = tmp_path / "dd.docx"
    res = export_docx(hub.store, ref, target_path=out)
    assert res.cited_slugs == ["wu22"]          # one paper, deduped
    import zipfile
    with zipfile.ZipFile(out) as z:
        body = z.read("word/document.xml").decode("utf-8")
        endnotes = z.read("word/endnotes.xml").decode("utf-8")
    assert body.count("endnoteReference") == 1   # consecutive marks collapsed
    assert endnotes.count('w:id="1"') == 1       # exactly one content endnote
    assert "Wu 2022 study" in endnotes

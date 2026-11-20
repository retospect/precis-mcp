"""Word-native citations (``citations='word'``): the sources part, the
``CITATION`` content controls and the ``BIBLIOGRAPHY`` control, read back from
the package the export writes. Nothing here opens Word: the format is pinned
by XML assertions, relationship and content-type checks, and a python-docx
reopen."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler
from precis.handlers.todo import TodoHandler

docx = pytest.importorskip("docx")  # python-docx (the `docx` extra)
from lxml import etree

from precis.export import docx as dx
from precis.export import wordcite
from precis.export.docx import export_docx

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_B = "http://schemas.openxmlformats.org/officeDocument/2006/bibliography"
_NS = {"w": _W, "b": _B}
_RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


# ── helpers ───────────────────────────────────────────────────────────


class _Store:
    """A store for the ctx-level tests: slug -> ref-ish, optional DOIs."""

    pool: Any = None

    def __init__(self, refs: dict[str, Any], dois: dict[str, str] | None = None):
        self._refs = {("paper", s): r for s, r in refs.items()}
        self._dois = dois or {}
        self.chunks = SimpleNamespace(chunk_text_by_id=lambda cid: f"passage {cid}")

    def get_ref(self, *, kind: str, id: int | str) -> Any:
        return self._refs.get((kind, str(id)))

    def identifiers_for_refs(self, ids: list[int]) -> dict[int, dict[str, str]]:
        by_id = {r.id: s for (_k, s), r in self._refs.items()}
        return {
            i: {"doi": self._dois[by_id[i]]} for i in ids if by_id.get(i) in self._dois
        }

    def resolve_handle(self, handle: str) -> Any:
        return SimpleNamespace(
            public_id="a1", chunk_id=int(handle[2:]), cite_public_id=None
        )


def _ref(i: int, slug: str, **kw: Any) -> Any:
    return SimpleNamespace(
        id=i,
        slug=slug,
        kind="paper",
        pdf_role=None,
        title=kw.pop("title", f"Title of {slug}"),
        authors=kw.pop("authors", [{"family": "Doe", "given": "J."}]),
        year=kw.pop("year", 2024),
        meta=kw.pop("meta", {}),
    )


def _ctx(store: _Store) -> Any:
    return dx._Ctx(
        store=store,
        known_handles=set(),
        word=True,
        doi_links=False,
        library_links=False,
        library_label="x",
        library_search_url="x",
    )


def _para_text(p: Any) -> str:
    """All visible text of a paragraph, including inside content controls
    (python-docx's ``paragraph.text`` skips those)."""
    return "".join(t.text or "" for t in p._p.iter(f"{{{_W}}}t"))


def _marks(root: Any) -> list[tuple[str, str]]:
    """``(instruction, cached text)`` of every citation content control."""
    out = []
    for sdt in root.iter(f"{{{_W}}}sdt"):
        if sdt.find("w:sdtPr/w:citation", _NS) is None:
            continue
        instr = "".join(e.text or "" for e in sdt.iter(f"{{{_W}}}instrText"))
        text = "".join(e.text or "" for e in sdt.iter(f"{{{_W}}}t"))
        out.append((instr, text))
    return out


def _render(text: str, ctx: Any) -> Any:
    para = docx.Document().add_paragraph()
    dx._render_inline(text, ctx, para)
    return para


def _seed(store: Any, slug: str, title: str, year: int, **kw: Any) -> int:
    return store.insert_ref(
        kind="paper", slug=slug, title=title, year=year, provider="manual", **kw
    ).id


def _new_draft(hub: Hub, draft: DraftHandler, name: str, *paragraphs: str) -> Any:
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id=name, title="T", project=pid)
    for text in paragraphs:
        draft.put(id=name, chunk_kind="paragraph", text=text, at={"last": True})
    return hub.live_store.get_ref(kind="draft", id=name)


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


# ── the whole package ─────────────────────────────────────────────────


def test_word_export_writes_sources_part_marks_and_bibliography(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    store = hub.live_store
    a = _seed(
        store,
        "kroto1985",
        "C60: Buckminsterfullerene",
        1985,
        authors=[{"family": "Kroto", "given": "H. W."}],
        meta={
            "journal": "Nature",
            "volume": "318",
            "number": "6042",
            "pages": "162-163",
        },
    )
    store.insert_ref_identifiers(a, [("doi", "10.1038/318162a0", "manual")])
    _seed(
        store,
        "krae1990",
        "Solid C60",
        1990,
        authors=[{"family": "Kraetschmer", "given": "W."}],
        meta={"journal": "Nature", "volume": "347"},
    )
    ref = _new_draft(
        hub,
        draft,
        "wd",
        "Fullerenes [§kroto1985~0][§krae1990~0] were found; later [§krae1990~0] too.",
    )
    out = tmp_path / "wd.docx"
    res = export_docx(store, ref, target_path=out, citations="word")
    assert res.cited_slugs == ["kroto1985", "krae1990"]

    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        body = z.read("word/document.xml")
        settings = z.read("word/settings.xml").decode("utf-8")
        ctypes = z.read("[Content_Types].xml").decode("utf-8")
        doc_rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
        item = z.read("customXml/item1.xml")
        item_rels = z.read("customXml/_rels/item1.xml.rels").decode("utf-8")
        props = z.read("customXml/itemProps1.xml")

    # Parts, relationships, content types.
    assert {"customXml/item1.xml", "customXml/itemProps1.xml"} <= names
    rel = re.search(r'<Relationship [^>]*customXml"[^>]*>', doc_rels)
    assert rel is not None and 'Target="../customXml/item1.xml"' in rel.group(0)
    assert f'Type="{_RT}/customXml"' in rel.group(0)
    assert f'Type="{_RT}/customXmlProps"' in item_rels
    assert 'Target="itemProps1.xml"' in item_rels
    assert (
        'PartName="/customXml/itemProps1.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.customXmlProperties+xml"'
    ) in ctypes or (
        'ContentType="application/vnd.openxmlformats-officedocument'
        '.customXmlProperties+xml" PartName="/customXml/itemProps1.xml"'
    ) in ctypes
    # item1.xml rides the package's default for ``.xml`` (application/xml).
    assert re.search(r'<Default [^>]*Extension="xml"[^>]*application/xml', ctypes) or (
        re.search(r'<Default [^>]*application/xml[^>]*Extension="xml"', ctypes)
    )
    assert "/customXml/item1.xml" not in ctypes  # no override needed

    # The sources.
    root = etree.fromstring(item)
    assert root.tag == f"{{{_B}}}Sources"
    assert root.get("SelectedStyle") == "\\IEEE2006OfficeOnline.xsl"
    assert root.get("StyleName") == "IEEE" and root.get("Version") == "2006"
    sources = root.findall("b:Source", _NS)
    assert [s.findtext("b:Tag", namespaces=_NS) for s in sources] == [
        "kroto1985",
        "krae1990",
    ]
    kroto = sources[0]
    assert kroto.findtext("b:SourceType", namespaces=_NS) == "JournalArticle"
    assert kroto.findtext("b:Title", namespaces=_NS) == "C60: Buckminsterfullerene"
    assert kroto.findtext("b:JournalName", namespaces=_NS) == "Nature"
    assert kroto.findtext("b:Year", namespaces=_NS) == "1985"
    assert kroto.findtext("b:Volume", namespaces=_NS) == "318"
    assert kroto.findtext("b:Issue", namespaces=_NS) == "6042"
    assert kroto.findtext("b:Pages", namespaces=_NS) == "162-163"
    assert kroto.findtext("b:DOI", namespaces=_NS) == "10.1038/318162a0"
    assert kroto.findtext("b:RefOrder", namespaces=_NS) == "1"
    assert sources[1].findtext("b:RefOrder", namespaces=_NS) == "2"
    person = kroto.find("b:Author/b:Author/b:NameList/b:Person", _NS)
    assert person is not None
    assert person.findtext("b:Last", namespaces=_NS) == "Kroto"
    assert person.findtext("b:First", namespaces=_NS) == "H. W."
    # No empty element anywhere (the second source has no issue / pages / DOI).
    assert sources[1].find("b:Issue", _NS) is None
    assert sources[1].find("b:DOI", _NS) is None
    assert all((e.text or "").strip() or len(e) for s in sources for e in s.iter())

    ds = "http://schemas.openxmlformats.org/officeDocument/2006/customXml"
    p = etree.fromstring(props)
    assert p.tag == f"{{{ds}}}datastoreItem"
    assert re.fullmatch(
        r"\{[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}\}", p.get(f"{{{ds}}}itemID") or ""
    )
    ref_el = p.find(f"{{{ds}}}schemaRefs/{{{ds}}}schemaRef")
    assert ref_el is not None and ref_el.get(f"{{{ds}}}uri") == _B

    # In-text marks: one grouped field, then the repeat's own.
    body_root = etree.fromstring(body)
    marks = _marks(body_root)
    assert marks == [
        (" CITATION kroto1985 \\m krae1990 \\l 1033 ", "[1, 2]"),
        (" CITATION krae1990 \\l 1033 ", "[2]"),
    ]
    ids = [
        int(e.get(f"{{{_W}}}val") or "x")
        for e in body_root.iterfind(".//w:sdt/w:sdtPr/w:id", _NS)
    ]
    assert len(ids) == len(set(ids)) == 3  # two marks + the bibliography

    # The reference list: one bibliography control, one field over its
    # paragraphs, each "[n]", a tab, the entry, in the Bibliography style.
    (bib,) = [
        s
        for s in body_root.iter(f"{{{_W}}}sdt")
        if s.find("w:sdtPr/w:bibliography", _NS) is not None
    ]
    paras = bib.findall("w:sdtContent/w:p", _NS)
    assert len(paras) == 2
    instrs = [e.text for e in bib.iter(f"{{{_W}}}instrText")]
    assert instrs == [" BIBLIOGRAPHY "]
    begins = [e.get(f"{{{_W}}}fldCharType") for e in bib.iterfind(".//w:fldChar", _NS)]
    assert begins == ["begin", "separate", "end"]
    assert paras[0].find(".//w:fldChar", _NS) is not None
    assert (
        paras[-1].findall(".//w:fldChar", _NS)[-1].get(f"{{{_W}}}fldCharType") == "end"
    )
    for para, n in zip(paras, (1, 2), strict=True):
        style = para.find("w:pPr/w:pStyle", _NS)
        assert style is not None and style.get(f"{{{_W}}}val") == "Bibliography"
        texts = [t.text for t in para.iter(f"{{{_W}}}t")]
        assert texts[0] == f"[{n}]" and para.find(".//w:tab", _NS) is not None
    assert "C60: Buckminsterfullerene" in "".join(
        t.text or "" for t in paras[0].iter(f"{{{_W}}}t")
    )
    # The list follows its "References" heading in the body.
    heading = bib.getprevious()
    assert heading is not None
    assert "".join(t.text or "" for t in heading.iter(f"{{{_W}}}t")) == "References"

    # No EndNote machinery, no research notes.
    assert "EN.CITE" not in body.decode("utf-8") and "EN.REFLIST" not in body.decode()
    assert "EN." not in settings and "docVars" not in settings
    assert "research-notes" not in body.decode("utf-8") + item.decode("utf-8")

    # Reopen with python-docx; the mark text is read through the controls.
    reopened = docx.Document(str(out))
    prose = next(q for q in reopened.paragraphs if "Fullerenes" in _para_text(q))
    assert _para_text(prose).startswith("Fullerenes [1, 2] were found; later [2] too.")


def test_word_export_is_byte_stable(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """No random id: two exports of one draft carry the same ids and GUIDs."""
    _seed(hub.live_store, "stab1", "Stable", 2001, meta={"journal": "J"})
    ref = _new_draft(hub, draft, "ws", "A [§stab1~0].")
    parts = []
    for name in ("a.docx", "b.docx"):
        export_docx(hub.live_store, ref, target_path=tmp_path / name, citations="word")
        with zipfile.ZipFile(tmp_path / name) as z:
            parts.append(
                tuple(
                    z.read(n)
                    for n in (
                        "word/document.xml",
                        "customXml/item1.xml",
                        "customXml/itemProps1.xml",
                    )
                )
            )
    assert parts[0] == parts[1]


def test_word_export_of_an_uncited_draft_keeps_the_templates_empty_sources(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    ref = _new_draft(hub, draft, "wn", "Nothing cited here.")
    out = tmp_path / "wn.docx"
    export_docx(hub.live_store, ref, target_path=out, citations="word")
    with zipfile.ZipFile(out) as z:
        items = [n for n in z.namelist() if re.fullmatch(r"customXml/item\d+\.xml", n)]
        assert items == ["customXml/item1.xml"]
        root = etree.fromstring(z.read(items[0]))
        assert root.findall("b:Source", _NS) == []
        assert b"<w:citation" not in z.read("word/document.xml")


def test_word_export_fills_the_one_sources_part_the_template_has(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """python-docx's template ships an empty bibliography part: filled, not
    duplicated (Word reads one)."""
    _seed(hub.live_store, "one1", "One", 2001, meta={"journal": "J"})
    ref = _new_draft(hub, draft, "wo", "A [§one1~0].")
    out = tmp_path / "wo.docx"
    export_docx(hub.live_store, ref, target_path=out, citations="word")
    with zipfile.ZipFile(out) as z:
        assert [n for n in z.namelist() if n.startswith("customXml/item")] == [
            "customXml/item1.xml",
            "customXml/itemProps1.xml",
        ]
        rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
    assert len(re.findall(r'relationships/customXml"', rels)) == 1


# ── source types ──────────────────────────────────────────────────────


def _source(store: _Store, slug: str) -> dict[str, Any]:
    source = dx._resolve_source(store, slug)
    assert source is not None
    return source


@pytest.mark.parametrize(
    ("meta", "kind", "expected", "element", "value"),
    [
        ({"journal": "Nature"}, "paper", "JournalArticle", "JournalName", "Nature"),
        (
            {"entry_type": "book-chapter", "container_title": "Handbook"},
            "paper",
            "BookSection",
            "BookTitle",
            "Handbook",
        ),
        (
            {"entry_type": "book", "publisher": "Wiley"},
            "paper",
            "Book",
            "Publisher",
            "Wiley",
        ),
        (
            {"entry_type": "proceedings-article", "container_title": "Proc. ACS"},
            "paper",
            "ConferenceProceedings",
            "ConferenceName",
            "Proc. ACS",
        ),
        (
            {"entry_type": "report", "institution": "NIST"},
            "paper",
            "Report",
            "Institution",
            "NIST",
        ),
        (
            {"entry_type": "posted-content"},
            "paper",
            "InternetSite",
            "URL",
            "https://doi.org/10.1/x",
        ),
        (
            {"record_type": "software", "publisher": "Zenodo"},
            "paper",
            "Misc",
            "Publisher",
            "Zenodo",
        ),
        (
            {"record_type": "dataset", "publisher": "Zenodo"},
            "paper",
            "Misc",
            "Publisher",
            "Zenodo",
        ),
        ({}, "patent", "Patent", "Title", "Title of s1"),
        ({"vendor": "Espressif"}, "datasheet", "Misc", "Publisher", "Espressif"),
    ],
)
def test_word_source_type_mapping(
    meta: dict[str, Any], kind: str, expected: str, element: str, value: str
) -> None:
    ref = _ref(5, "s1", meta=meta)
    ref.kind = kind
    store = _Store({}, {"s1": "10.1/x"})
    store._refs[(kind, "s1")] = ref
    source = _source(store, "s1")
    root = etree.fromstring(wordcite.sources_xml([("s1", 3, source)]))
    (src,) = root.findall("b:Source", _NS)
    assert src.findtext("b:SourceType", namespaces=_NS) == expected
    assert src.findtext(f"b:{element}", namespaces=_NS) == value
    assert src.findtext("b:RefOrder", namespaces=_NS) == "3"
    assert src.findtext("b:DOI", namespaces=_NS) == "10.1/x"


def test_word_thesis_is_a_report() -> None:
    assert wordcite.source_type({"bib_type": "thesis"}) == "Report"


def test_word_source_omits_empty_elements_and_escapes() -> None:
    xml = wordcite.source_xml(
        {
            "kind": "paper",
            "bib_type": "article",
            "title": "Cu&amp;Zn <i>in</i> a < b",
            "authors": [{"name": "Plato"}, {"name": "Marie Curie"}, {"name": ""}],
            "year": None,
            "journal": "",
            "volume": None,
        },
        "tag1",
        4,
    )
    src = etree.fromstring(f'<r xmlns:b="{_B}">{xml}</r>').find("b:Source", _NS)
    assert src is not None
    assert [c.tag.split("}")[1] for c in src] == [
        "Tag",
        "SourceType",
        "Guid",
        "Author",
        "Title",
        "RefOrder",
    ]
    assert src.findtext("b:Title", namespaces=_NS) == "Cu&Zn in a < b"
    people = [
        (p.findtext("b:Last", namespaces=_NS), p.findtext("b:First", namespaces=_NS))
        for p in src.iterfind(".//b:Person", _NS)
    ]
    assert people == [("Plato", None), ("Curie", "Marie")]


def test_word_tags_are_unique_alphanumeric_and_guids_follow_the_tag() -> None:
    wc = wordcite.WordCitations()
    tags = [wc.tag(s) for s in ("a-1", "a1", "Smith_2020:x", "!!!")]
    assert tags == ["a1", "a12", "Smith2020x", "Src"]
    assert all(re.fullmatch(r"[A-Za-z0-9]+", t) for t in tags)
    assert wc.tag("a-1") == "a1"  # stable per slug
    g = wordcite._guid("a1")
    assert g == wordcite._guid("a1") != wordcite._guid("a12")
    assert re.fullmatch(r"\{[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}\}", g)


def test_word_tag_with_a_leading_digit_gets_a_letter() -> None:
    wc = wordcite.WordCitations()
    assert wc.tag("2020smith") == "R2020smith"
    assert wc.tag("42") == "R42"
    assert wc.tag("-7x") == "R7x"


def test_word_two_papers_never_share_a_tag_or_guid() -> None:
    slugs = ["a-1", "a1", "a12", "5", "R5", "r5", "!!!", "???", "ab", "a_b"]
    wc = wordcite.WordCitations()
    tags = [wc.tag(s) for s in slugs]
    assert len(set(tags)) == len(slugs), tags
    assert all(re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", t) for t in tags)
    guids = [wordcite._guid(t) for t in tags]
    assert len(set(guids)) == len(slugs)
    # the same paper always gets the same ones, in a fresh export too
    again = wordcite.WordCitations()
    assert [again.tag(s) for s in slugs] == tags
    assert [wc.tag(s) for s in reversed(slugs)] == list(reversed(tags))
    # the Guid in the sources part is derived from the final tag
    entries = [(t, i, {"title": s}) for i, (t, s) in enumerate(zip(tags, slugs), 1)]
    root = etree.fromstring(wordcite.sources_xml(entries))
    assert [g.text for g in root.iterfind(".//b:Guid", _NS)] == guids


def _src(xml: str) -> Any:
    return etree.fromstring(f'<r xmlns:b="{_B}">{xml}</r>').find("b:Source", _NS)


def _source_of(authors: list[Any], **kw: Any) -> str:
    return wordcite.source_xml(
        {"kind": "paper", "bib_type": "report", "title": "T", "authors": authors, **kw},
        "t",
        1,
    )


def test_word_patent_people_are_inventors_and_the_number_is_written() -> None:
    src = _src(
        wordcite.source_xml(
            {
                "kind": "patent",
                "title": "Pump",
                "authors": [{"name": "Doe, Jane"}],
                "patent_number": "6368648B1",
            },
            "p1",
            1,
        )
    )
    assert src.findtext("b:SourceType", namespaces=_NS) == "Patent"
    assert src.findtext("b:PatentNumber", namespaces=_NS) == "6368648B1"
    assert src.find("b:Author/b:Author", _NS) is None
    last = src.findtext(
        "b:Author/b:Inventor/b:NameList/b:Person/b:Last", namespaces=_NS
    )
    assert last == "Doe"
    # other types keep b:Author/b:Author and carry no patent number
    art = _src(
        wordcite.source_xml(
            {
                "kind": "paper",
                "bib_type": "article",
                "title": "T",
                "authors": [{"name": "Doe, J."}],
                "patent_number": "X",
            },
            "p2",
            2,
        )
    )
    assert art.find("b:Author/b:Author/b:NameList", _NS) is not None
    assert art.find("b:Author/b:Inventor", _NS) is None
    assert art.find("b:PatentNumber", _NS) is None


def test_word_patent_number_comes_from_the_resolved_patent_record() -> None:
    meta = {"country": "us", "doc_number": "6368648", "kind_code": "b1"}
    ref = _ref(5, "us6368648b1", meta=meta)
    ref.kind = "patent"
    store = _Store({})
    store._refs[("patent", "us6368648b1")] = ref
    source = dx._resolve_source(store, "us6368648b1")
    assert source is not None
    assert source["patent_number"] == "6368648B1"
    src = _src(wordcite.source_xml(source, "p", 1))
    assert src.findtext("b:PatentNumber", namespaces=_NS) == "6368648B1"


def test_word_marked_organisation_is_corporate_unmarked_name_stays_a_person() -> None:
    corp = (
        "<b:Author><b:Author><b:Corporate>World Health Organization"
        "</b:Corporate></b:Author></b:Author>"
    )
    for author in (
        {"literal": "World Health Organization"},
        {"name": "{World Health Organization}"},
        {"name": "World Health Organization", "kind": "organization"},
        {"name": "World Health Organization", "type": "Corporate"},
    ):
        assert corp in _source_of([author]), author
    # the record does not mark it: a person, whatever the spelling
    src = _src(_source_of([{"name": "World Health Organization"}]))
    assert src.find("b:Author/b:Author/b:Corporate", _NS) is None
    assert src.findtext(".//b:Person/b:Last", namespaces=_NS) == "Organization"


def test_word_datasheet_vendor_and_publisher_as_author_are_corporate() -> None:
    ds = _src(
        wordcite.source_xml(
            {
                "kind": "datasheet",
                "title": "ESP32",
                "authors": [],
                "vendor": "Espressif",
                "publisher": "Espressif",
            },
            "d",
            1,
        )
    )
    assert ds.findtext("b:Author/b:Author/b:Corporate", namespaces=_NS) == "Espressif"
    sw = _src(_source_of([{"name": "Zenodo"}], bib_type="software", publisher="Zenodo"))
    assert sw.findtext("b:Author/b:Author/b:Corporate", namespaces=_NS) == "Zenodo"
    # beside persons the name list cannot hold a Corporate: a Last-only person
    mixed = _src(_source_of([{"family": "Doe", "given": "J."}, {"literal": "WHO"}]))
    assert mixed.find("b:Author/b:Author/b:Corporate", _NS) is None
    assert [
        (p.findtext("b:Last", namespaces=_NS), p.findtext("b:First", namespaces=_NS))
        for p in mixed.iterfind(".//b:Person", _NS)
    ] == [("Doe", "J."), ("WHO", None)]


def test_word_missing_year_warning_says_the_element_is_omitted() -> None:
    ctx = _ctx(_Store({"a1": _ref(1, "a1", year=None)}))
    _render("See [§a1].", ctx)
    (warning,) = [w for w in ctx.warnings if "a1" in w]
    assert "no <b:Year> element written" in warning
    assert "empty" not in warning


# ── marks ─────────────────────────────────────────────────────────────


def _three_ctx() -> Any:
    refs = {
        "a1": _ref(1, "a1"),
        "b2": _ref(2, "b2"),
        "c3": _ref(3, "c3"),
        "d4": _ref(4, "d4"),
    }
    return _ctx(_Store(refs))


def test_word_adjacent_cites_are_one_grouped_field() -> None:
    ctx = _three_ctx()
    para = _render("See [§a1][§b2] here.", ctx)
    assert _marks(para._p) == [(" CITATION a1 \\m b2 \\l 1033 ", "[1, 2]")]
    assert _para_text(para) == "See [1, 2] here."


def test_word_three_consecutive_cites_print_a_range() -> None:
    ctx = _three_ctx()
    para = _render("See [§a1][§b2][§c3] here.", ctx)
    assert _marks(para._p) == [(" CITATION a1 \\m b2 \\m c3 \\l 1033 ", "[1–3]")]


def test_word_mark_is_a_content_control_holding_one_complex_field() -> None:
    ctx = _three_ctx()
    para = _render("See [§a1].", ctx)
    (sdt,) = para._p.findall("w:sdt", _NS)
    assert sdt.find("w:sdtPr/w:citation", _NS) is not None
    kinds = [
        e.tag.split("}")[1] + (e.get(f"{{{_W}}}fldCharType") or "")
        for e in sdt.iterfind("w:sdtContent/w:r/*", _NS)
        if e.tag != f"{{{_W}}}rPr"
    ]
    assert kinds == ["fldCharbegin", "instrText", "fldCharseparate", "t", "fldCharend"]
    assert sdt.find("w:sdtContent/w:r/w:rPr/w:noProof", _NS) is not None
    instr = sdt.find(".//w:instrText", _NS)
    assert instr is not None
    assert instr.get("{http://www.w3.org/XML/1998/namespace}space") == "preserve"


def _si_ctx(monkeypatch: pytest.MonkeyPatch) -> Any:
    """``parent24si`` is the SI record of ``parent24``; ``other14`` a plain
    paper cited second."""
    from precis.store import si_links

    parent = _ref(1, "parent24")
    si = _ref(2, "parent24si")
    si.pdf_role = "supplement"
    other = _ref(3, "other14")
    other_si = _ref(4, "other14si")
    other_si.pdf_role = "supplement"

    class _Conn:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *a: object) -> None:
            return None

    store = _Store(
        {
            "parent24": parent,
            "parent24si": si,
            "other14": other,
            "other14si": other_si,
        }
    )
    store.pool = SimpleNamespace(connection=lambda: _Conn())
    monkeypatch.setattr(
        si_links,
        "supplement_parent",
        lambda _c, rid: {2: (1, "parent24"), 4: (3, "other14")}.get(rid),
    )
    return _ctx(store)


def test_word_si_cite_carries_its_postnote_as_the_suffix_switch(monkeypatch) -> None:
    ctx = _si_ctx(monkeypatch)
    para = _render("Data [§parent24si].", ctx)
    assert ctx.cited == ["parent24"]
    assert _marks(para._p) == [(' CITATION parent24 \\s " (SI)" \\l 1033 ', "[1] (SI)")]


def _endnote_text(text: str, store: Any) -> str:
    """The visible text the EndNote mode prints for the same input."""
    ctx = dx._Ctx(
        store=store,
        known_handles=set(),
        endnote=True,
        doi_links=False,
        library_links=False,
        library_label="x",
        library_search_url="x",
    )
    ctx.cite_number("parent24")
    ctx.cite_number("other14")
    return _para_text(_render(text, ctx))


@pytest.mark.parametrize(
    ("text", "instr", "mark"),
    [
        # a single SI source
        (
            "Data [§parent24si] here.",
            ' CITATION parent24 \\s " (SI)" \\l 1033 ',
            "[1] (SI)",
        ),
        # an all-SI pair: one mark, the postnote after each SI source
        (
            "Data [§parent24si][§other14si] here.",
            ' CITATION parent24 \\s " (SI)" \\m other14 \\s " (SI)" \\l 1033 ',
            "[1, 2] (SI)",
        ),
        # a mixed group: the postnote after the SI source only
        (
            "Data [§other14][§parent24si] here.",
            ' CITATION parent24 \\s " (SI)" \\m other14 \\l 1033 ',
            "[1 (SI), 2]",
        ),
    ],
)
def test_word_si_group_is_one_mark_like_every_other_mode(
    monkeypatch: pytest.MonkeyPatch, text: str, instr: str, mark: str
) -> None:
    """The switches after a source tag belong to that source and ``\\m``
    starts the next: one field per group, the cached text the mark the
    EndNote mode prints."""
    ctx = _si_ctx(monkeypatch)
    ctx.cite_number("parent24")
    ctx.cite_number("other14")
    para = _render(text, ctx)
    assert _marks(para._p) == [(instr, mark)]
    assert _para_text(para) == f"Data {mark} here."
    assert _endnote_text(text, ctx.store) == f"Data {mark} here."


def test_word_dropped_repeat_prints_no_second_mark_and_closes_up() -> None:
    ctx = _three_ctx()
    para = _render("First [§a1] then more [§a1] again.", ctx)
    assert len(_marks(para._p)) == 1  # the repeat is not written
    assert _para_text(para) == "First [1] then more again."
    # ... and the mark repeats when asked for.
    ctx = _three_ctx()
    ctx.repeat_cites = True
    para = _render("First [§a1] then more [§a1] again.", ctx)
    assert len(_marks(para._p)) == 2


def test_word_dropped_repeat_straight_after_a_mark_trims_nothing() -> None:
    """The mark is a content control, not a run: the run before it must not
    be trimmed for the dropped repeat (the EndNote field ends in a run)."""
    ctx = _three_ctx()
    para = _render("First [§a1][§a1] then.", ctx)
    assert _para_text(para) == "First [1] then."


def test_word_same_doi_alias_is_one_source() -> None:
    store = _Store(
        {"a1": _ref(1, "a1"), "b2": _ref(2, "b2", year=2023)},
        {"a1": "10.1/X", "b2": "https://doi.org/10.1/x"},
    )
    ctx = _ctx(store)
    para = _render("Both [§a1] and [§b2].", ctx)
    assert ctx.cited == ["a1"]
    # both marks name the one source
    assert _marks(para._p) == [(" CITATION a1 \\l 1033 ", "[1]")] * 2
    doc = docx.Document()
    dx._install_word_sources(doc, ctx)
    part = next(
        r.target_part
        for r in doc.part.rels.values()
        if r.reltype.endswith("/customXml")
    )
    root = etree.fromstring(part.blob)
    assert [
        s.findtext("b:Tag", namespaces=_NS) for s in root.findall("b:Source", _NS)
    ] == ["a1"]


def test_word_unresolved_slug_degrades_to_a_plain_marker() -> None:
    ctx = _ctx(_Store({"a1": _ref(1, "a1")}))
    para = _render("See [§a1][§nosuch9].", ctx)
    assert ctx.cited == ["a1", "nosuch9"]
    assert _marks(para._p) == [(" CITATION a1 \\l 1033 ", "[1]")]
    assert "[2]" in _para_text(para)
    assert any("nosuch9" in w for w in ctx.warnings)
    doc = docx.Document()
    dx._install_word_sources(doc, ctx)  # the unresolved one has no source
    part = next(
        r.target_part
        for r in doc.part.rels.values()
        if r.reltype.endswith("/customXml")
    )
    tags = [
        s.findtext("b:Tag", namespaces=_NS)
        for s in etree.fromstring(part.blob).findall("b:Source", _NS)
    ]
    assert tags == ["a1"]


def test_word_mode_leaves_no_notes_and_no_endnote_state() -> None:
    ctx = _three_ctx()
    ctx.store = _Store({"a1": _ref(1, "a1")})
    para = _render("Quoted [pc10].", ctx)
    assert "research-notes" not in etree.tostring(para._p).decode()
    assert ctx._kept_field is None


# ── what reads the paragraph after a mark ─────────────────────────────


def test_sentence_start_and_term_context_see_the_mark_text() -> None:
    ctx = _three_ctx()
    para = docx.Document().add_paragraph()
    para.add_run("Done. ")
    assert dx._at_sentence_start(para)
    dx._render_inline("[§a1]", ctx, para)
    # python-docx's own reading misses the control; the export's does not
    assert para.text == "Done. "
    assert dx._paragraph_text(para) == "Done. [1]"
    assert not dx._at_sentence_start(para)


def test_rstrip_last_run_leaves_the_run_before_a_mark_alone() -> None:
    ctx = _three_ctx()
    para = docx.Document().add_paragraph()
    para.add_run("see ")
    dx._render_inline("[§a1]", ctx, para)
    dx._rstrip_last_run(para)
    assert para.runs[-1].text == "see "


# ── the controls ──────────────────────────────────────────────────────


def test_word_is_an_implemented_mode() -> None:
    from precis.export import IMPLEMENTED_CITATION_MODES, guard_citation_mode

    assert "word" in IMPLEMENTED_CITATION_MODES
    guard_citation_mode("word")

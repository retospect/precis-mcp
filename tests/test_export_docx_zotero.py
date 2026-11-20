"""Zotero fields (``citations='zotero'``): the ``ZOTERO_ITEM`` citation fields
with embedded CSL JSON, the ``ZOTERO_BIBL`` list and the document preferences,
read back from the package the export writes. Nothing here opens Word or
Zotero: the format is pinned by JSON and XML assertions, relationship and
content-type checks, and a python-docx reopen."""

from __future__ import annotations

import json
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
from precis.export import zoterocite
from precis.export.docx import export_docx

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_CP = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
_VT = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
_NS = {"w": _W, "cp": _CP, "vt": _VT}
_RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_SCHEMA = (
    "https://github.com/citation-style-language/schema/raw/master/csl-citation.json"
)
_ITEM = " ADDIN ZOTERO_ITEM CSL_CITATION "
_BIBL = ' ADDIN ZOTERO_BIBL {"uncited":[],"omitted":[],"custom":[]} CSL_BIBLIOGRAPHY '


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
        zotero=True,
        doi_links=False,
        library_links=False,
        library_label="x",
        library_search_url="x",
    )


def _fields(root: Any) -> list[tuple[str, str]]:
    """``(instruction, cached text)`` of every complex field under ``root``,
    read run by run (a field here is flat in the paragraph)."""
    out: list[tuple[str, str]] = []
    instr: list[str] = []
    cached: list[str] = []
    state = "idle"
    for run in root.iter(f"{{{_W}}}r"):
        for el in run:
            if el.tag == f"{{{_W}}}fldChar":
                kind = el.get(f"{{{_W}}}fldCharType")
                if kind == "begin":
                    state, instr, cached = "instr", [], []
                elif kind == "separate":
                    state = "cached"
                elif kind == "end":
                    out.append(("".join(instr), "".join(cached)))
                    state = "idle"
            elif el.tag == f"{{{_W}}}instrText" and state == "instr":
                instr.append(el.text or "")
            elif el.tag == f"{{{_W}}}t" and state == "cached":
                cached.append(el.text or "")
    return out


def _marks(root: Any) -> list[tuple[dict[str, Any], str]]:
    """``(parsed CSL_CITATION JSON, cached text)`` of every in-text mark."""
    out = []
    for instr, cached in _fields(root):
        if instr.startswith(_ITEM):
            assert instr.endswith("} ")  # the JSON, then one trailing space
            out.append((json.loads(instr[len(_ITEM) :]), cached))
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


def _pref(blob: bytes) -> tuple[str, list[tuple[str, str, str]]]:
    """The reassembled preference string and the ``(name, pid, fmtid)`` of
    each ``ZOTERO_PREF_n`` property, in document order."""
    root = etree.fromstring(blob)
    props = root.findall("cp:property", _NS)
    text = "".join(p.findtext("vt:lpwstr", namespaces=_NS) or "" for p in props)
    return text, [
        (p.get("name") or "", p.get("pid") or "", p.get("fmtid") or "") for p in props
    ]


# ── the whole package ─────────────────────────────────────────────────


def test_zotero_export_writes_fields_list_and_preferences(
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
    b = _seed(
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
        "zd",
        "Fullerenes [§kroto1985~0][§krae1990~0] were found; later [§krae1990~0] too.",
    )
    out = tmp_path / "zd.docx"
    res = export_docx(store, ref, target_path=out, citations="zotero")
    assert res.cited_slugs == ["kroto1985", "krae1990"]

    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        body = z.read("word/document.xml")
        settings = z.read("word/settings.xml").decode("utf-8")
        ctypes = z.read("[Content_Types].xml").decode("utf-8")
        root_rels = z.read("_rels/.rels").decode("utf-8")
        custom = z.read("docProps/custom.xml")
        sources = z.read("customXml/item1.xml").decode("utf-8")

    # Parts, relationships, content types.
    assert "docProps/custom.xml" in names
    rel = re.search(r'<Relationship [^>]*custom-properties"[^>]*>', root_rels)
    assert rel is not None and 'Target="docProps/custom.xml"' in rel.group(0)
    assert f'Type="{_RT}/custom-properties"' in rel.group(0)
    assert re.search(
        r'<Override [^>]*PartName="/docProps/custom.xml"[^>]*'
        r'ContentType="application/vnd.openxmlformats-officedocument'
        r'.custom-properties\+xml"',
        ctypes,
    ) or re.search(
        r'<Override [^>]*ContentType="application/vnd.openxmlformats-officedocument'
        r'.custom-properties\+xml"[^>]*PartName="/docProps/custom.xml"',
        ctypes,
    )

    # In-text marks: one grouped field, then the repeat's own. Each holds
    # its records' ids and embedded items.
    body_root = etree.fromstring(body)
    marks = _marks(body_root)
    assert [m[1] for m in marks] == ["[1, 2]", "[2]"]
    group, repeat = (m[0] for m in marks)
    assert group["schema"] == _SCHEMA
    assert group["properties"] == {
        "formattedCitation": "[1, 2]",
        "plainCitation": "[1, 2]",
        "noteIndex": 0,
    }
    assert [i["id"] for i in group["citationItems"]] == [a, b]
    assert [i["id"] for i in repeat["citationItems"]] == [b]
    assert all("suffix" not in i for i in group["citationItems"])
    assert re.fullmatch(r"[a-z0-9]{8}", group["citationID"])
    assert group["citationID"] != repeat["citationID"]
    kroto = group["citationItems"][0]
    (uri,) = kroto["uris"]
    assert re.fullmatch(
        r"http://zotero\.org/users/local/[a-z0-9]{8}/items/[2-9A-Z]{8}", uri
    )
    assert kroto["itemData"] == {
        "id": a,
        "type": "article-journal",
        "title": "C60: Buckminsterfullerene",
        "container-title": "Nature",
        "author": [{"family": "Kroto", "given": "H. W."}],
        "issued": {"date-parts": [[1985]]},
        "volume": "318",
        "issue": "6042",
        "page": "162-163",
        "DOI": "10.1038/318162a0",
    }

    # The field is flat in the paragraph: python-docx reads the cached text.
    reopened = docx.Document(str(out))
    prose = next(q for q in reopened.paragraphs if "Fullerenes" in q.text)
    assert prose.text.startswith("Fullerenes [1, 2] were found; later [2] too.")
    assert body_root.find(".//w:sdt", _NS) is None  # no content control

    # The reference list: one field over the Bibliography paragraphs, each
    # "[n]", a tab, the entry, after the heading.
    fields = _fields(body_root)
    assert [f[0] for f in fields if "ZOTERO_BIBL" in f[0]] == [_BIBL]
    lists = [
        q
        for q in body_root.iterfind(".//w:p", _NS)
        if [s.get(f"{{{_W}}}val") for s in q.iterfind("w:pPr/w:pStyle", _NS)]
        == ["Bibliography"]
    ]
    assert len(lists) == 2
    begins = [
        e.get(f"{{{_W}}}fldCharType")
        for q in lists
        for e in q.iterfind(".//w:fldChar", _NS)
    ]
    assert begins == [
        "begin",
        "separate",
        "end",
    ]  # opens in the first, ends in the last
    assert lists[0].find(".//w:fldChar", _NS) is not None
    assert (
        lists[-1].findall(".//w:fldChar", _NS)[-1].get(f"{{{_W}}}fldCharType") == "end"
    )
    for para, n in zip(lists, (1, 2), strict=True):
        texts = [t.text for t in para.iter(f"{{{_W}}}t")]
        assert texts[0] == f"[{n}]" and para.find(".//w:tab", _NS) is not None
    heading = lists[0].getprevious()
    assert heading is not None
    assert "".join(t.text or "" for t in heading.iter(f"{{{_W}}}t")) == "References"

    # Preferences: the string reassembles from the properties.
    text, props = _pref(custom)
    assert text.startswith('<data data-version="3" zotero-version="6.0.30">')
    assert re.search(r'<session id="[a-z0-9]{8}"/>', text)
    assert text.endswith(
        '<style id="http://www.zotero.org/styles/ieee" locale="en-US" '
        'hasBibliography="1" bibliographyStyleHasBeenSet="0"/><prefs>'
        '<pref name="fieldType" value="Field"/></prefs></data>'
    )
    assert [p[0] for p in props] == [
        f"ZOTERO_PREF_{i}" for i in range(1, len(props) + 1)
    ]
    assert [p[1] for p in props] == [str(i) for i in range(2, len(props) + 2)]
    assert {p[2] for p in props} == {"{D5CDD505-2E9C-101B-9397-08002B2CF9AE}"}
    root = etree.fromstring(custom)
    values = [p.findtext("vt:lpwstr", namespaces=_NS) or "" for p in root]
    assert all(0 < len(v) <= 255 for v in values)
    assert len(values) == -(-len(text) // 255)  # no more pieces than needed

    # No EndNote machinery, no research notes, no Word sources in this mode
    # (the template's empty sources part stays empty).
    text_body = body.decode("utf-8")
    assert "EN.CITE" not in text_body and "EN.REFLIST" not in text_body
    assert "EN." not in settings and "docVars" not in settings
    assert "research-notes" not in text_body
    assert "<b:Source>" not in sources and " CITATION " not in text_body


def test_zotero_export_is_byte_stable(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    """No random id: two exports of one draft carry the same ids."""
    _seed(hub.live_store, "stab1", "Stable", 2001, meta={"journal": "J"})
    ref = _new_draft(hub, draft, "zs", "A [§stab1~0].")
    parts = []
    for name in ("a.docx", "b.docx"):
        export_docx(
            hub.live_store, ref, target_path=tmp_path / name, citations="zotero"
        )
        with zipfile.ZipFile(tmp_path / name) as z:
            parts.append(
                tuple(z.read(n) for n in ("word/document.xml", "docProps/custom.xml"))
            )
    assert parts[0] == parts[1]


def test_zotero_export_of_an_uncited_draft_has_no_preferences(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    ref = _new_draft(hub, draft, "zn", "Nothing cited here.")
    out = tmp_path / "zn.docx"
    export_docx(hub.live_store, ref, target_path=out, citations="zotero")
    with zipfile.ZipFile(out) as z:
        assert "docProps/custom.xml" not in z.namelist()
        assert b"ZOTERO" not in z.read("word/document.xml")


@pytest.mark.parametrize(
    ("workspace_style", "style_id"),
    [
        ("", "ieee"),
        ("numeric-comp", "ieee"),
        ("no-such-style", "ieee"),
        ("chem-acs", "american-chemical-society"),
        ("chem-rsc", "royal-society-of-chemistry"),
        ("nature", "nature"),
    ],
)
def test_zotero_preferences_follow_the_drafts_bibliography_style(
    draft: DraftHandler,
    hub: Hub,
    tmp_path: Path,
    workspace_style: str,
    style_id: str,
) -> None:
    import dataclasses

    _seed(hub.live_store, "sty1", "Styled", 2001, meta={"journal": "J"})
    ref = _new_draft(hub, draft, "zst", "A [§sty1~0].")
    if workspace_style:
        ref = dataclasses.replace(
            ref,
            meta={
                **(ref.meta or {}),
                "workspace": {"path": "p", "style": workspace_style},
            },
        )
    out = tmp_path / "zst.docx"
    res = export_docx(hub.live_store, ref, target_path=out, citations="zotero")
    with zipfile.ZipFile(out) as z:
        text, _props = _pref(z.read("docProps/custom.xml"))
    assert f'<style id="http://www.zotero.org/styles/{style_id}" ' in text
    # an unknown style warns exactly as the other modes do
    assert any("no-such-style" in w for w in res.warnings) == (
        workspace_style == "no-such-style"
    )


def test_zotero_title_with_json_and_xml_specials_survives_the_saved_field(
    tmp_path: Path,
) -> None:
    """A title holding ``"``, ``\\``, ``{``, ``}``, ``&`` and ``<`` goes through
    the written field and the JSON read back from the saved file's
    ``w:instrText`` is identical."""
    title = 'A "quoted" \\ title {x} & y < z'
    source = {
        "kind": "paper",
        "bib_type": "article",
        "rec_number": 7,
        "title": title,
        "journal": 'J & \\ "k" {k}',
        "year": 2001,
    }
    doc = docx.Document()
    para = doc.add_paragraph()
    zoterocite.add_citation(
        para, zoterocite.ZoteroCitations(), [(source, 1, False)], "[1]"
    )
    path = tmp_path / "t.docx"
    doc.save(str(path))
    with zipfile.ZipFile(path) as z:
        root = etree.fromstring(z.read("word/document.xml"))
    (instr, cached) = _fields(root)[0]
    assert instr.startswith(_ITEM) and cached == "[1]"
    obj = json.loads(instr[len(_ITEM) :])
    item = obj["citationItems"][0]["itemData"]
    assert item["title"] == title
    assert item["container-title"] == 'J & \\ "k" {k}'


# ── CSL items ─────────────────────────────────────────────────────────


def test_zotero_title_keeps_its_markup(tmp_path: Path) -> None:
    ref = _ref(
        5,
        "m1",
        title="C<sub>60</sub> and <i>Nature</i> of $\\mathrm{H_2O}$ &amp; <b>x</b>",
        meta={"journal": "<i>J. Chem.</i> <scp>Phys</scp>"},
    )
    store = _Store({"m1": ref})
    item = _item(store, "m1")
    assert item["title"] == (
        "C<sub>60</sub> and <i>Nature</i> of H<sub>2</sub>O & <b>x</b>"
    )
    assert item["container-title"] == (
        '<i>J. Chem.</i> <span style="font-variant:small-caps;">Phys</span>'
    )
    # a plain title is unchanged
    plain = _item(_Store({"p1": _ref(6, "p1", title="Plain title: no markup")}), "p1")
    assert plain["title"] == "Plain title: no markup"


def test_zotero_csl_markup_wraps_each_styled_run() -> None:
    runs = [
        ("a", frozenset()),
        ("b", frozenset({"i"})),
        ("c", frozenset({"i"})),
        ("d", frozenset({"b", "i"})),
        ("e", frozenset({"sup"})),
        ("f", frozenset({"sc"})),
    ]
    assert zoterocite.csl_markup(runs) == (
        "a<i>bc</i><i><b>d</b></i><sup>e</sup>"
        '<span style="font-variant:small-caps;">f</span>'
    )


def test_zotero_url_is_left_out_beside_a_doi() -> None:
    both = zoterocite.csl_item(
        {
            "rec_number": 1,
            "title": "T",
            "doi": "10.1/x",
            "url": "https://doi.org/10.1/x",
        }
    )
    assert both["DOI"] == "10.1/x" and "URL" not in both
    arxiv = zoterocite.csl_item(
        {"rec_number": 1, "title": "T", "url": "https://arxiv.org/abs/2101.00001"}
    )
    assert arxiv["URL"] == "https://arxiv.org/abs/2101.00001" and "DOI" not in arxiv


def _item(store: _Store, slug: str) -> dict[str, Any]:
    source = dx._resolve_source(store, slug)
    assert source is not None
    return zoterocite.csl_item(source)


@pytest.mark.parametrize(
    ("meta", "kind", "ctype", "member", "value"),
    [
        (
            {"journal": "Nature"},
            "paper",
            "article-journal",
            "container-title",
            "Nature",
        ),
        (
            {"entry_type": "book-chapter", "container_title": "Handbook"},
            "paper",
            "chapter",
            "container-title",
            "Handbook",
        ),
        (
            {"entry_type": "book", "publisher": "Wiley"},
            "paper",
            "book",
            "publisher",
            "Wiley",
        ),
        (
            {"entry_type": "proceedings-article", "container_title": "Proc. ACS"},
            "paper",
            "paper-conference",
            "container-title",
            "Proc. ACS",
        ),
        (
            {"entry_type": "report", "institution": "NIST"},
            "paper",
            "report",
            "publisher",
            "NIST",
        ),
        (
            {"entry_type": "posted-content"},
            "paper",
            "webpage",
            "DOI",
            "10.1/x",
        ),
        (
            {"record_type": "software", "publisher": "Zenodo"},
            "paper",
            "software",
            "publisher",
            "Zenodo",
        ),
        (
            {"record_type": "dataset", "publisher": "Zenodo"},
            "paper",
            "dataset",
            "publisher",
            "Zenodo",
        ),
        ({}, "patent", "patent", "title", "Title of s1"),
        ({"vendor": "Espressif"}, "datasheet", "document", "publisher", "Espressif"),
    ],
)
def test_zotero_csl_type_mapping(
    meta: dict[str, Any], kind: str, ctype: str, member: str, value: str
) -> None:
    ref = _ref(5, "s1", meta=meta)
    ref.kind = kind
    store = _Store({}, {"s1": "10.1/x"})
    store._refs[(kind, "s1")] = ref
    item = _item(store, "s1")
    assert item["type"] == ctype
    assert item[member] == value
    assert item["id"] == 5 and item["DOI"] == "10.1/x"
    assert item["issued"] == {"date-parts": [[2024]]}


def test_zotero_thesis_is_a_thesis() -> None:
    assert zoterocite.csl_type({"bib_type": "thesis"}) == "thesis"
    assert zoterocite.csl_type({"bib_type": "nonsense"}) == "article-journal"
    item = zoterocite.csl_item(
        {
            "bib_type": "thesis",
            "rec_number": 3,
            "title": "T",
            "institution": "ETH Zurich",
            "publisher": "Other",
        }
    )
    assert item["type"] == "thesis"
    assert item["publisher"] == "ETH Zurich"  # the institution, as for a report


def test_zotero_item_omits_empty_members_and_keeps_a_fixed_order() -> None:
    item = zoterocite.csl_item(
        {
            "kind": "paper",
            "bib_type": "article",
            "rec_number": 9,
            "title": "Cu&amp;Zn <i>in</i> a < b\x0b",
            "authors": [],
            "year": None,
            "journal": "",
            "volume": None,
        }
    )
    assert list(item) == ["id", "type", "title"]
    assert item["title"] == "Cu&Zn in a < b"
    full = zoterocite.csl_item(
        {
            "kind": "paper",
            "bib_type": "article",
            "rec_number": 9,
            "title": "T",
            "authors": [{"family": "A", "given": "B"}],
            "year": "1999",
            "journal": "J",
            "volume": "1",
            "number": "2",
            "pages": "3-4",
            "doi": "10.1/x",
            "url": "https://x",
        }
    )
    assert list(full) == [
        "id",
        "type",
        "title",
        "container-title",
        "author",
        "issued",
        "volume",
        "issue",
        "page",
        "DOI",
    ]
    assert full["issued"] == {"date-parts": [[1999]]}
    # a real URL without a DOI stays, in the last place
    no_doi = zoterocite.csl_item(
        {"rec_number": 1, "title": "T", "url": "https://arxiv.org/abs/2101.00001"}
    )
    assert list(no_doi) == ["id", "type", "title", "URL"]


def test_zotero_volume_issue_and_pages_only_where_the_type_has_them() -> None:
    patent = zoterocite.csl_item(
        {
            "kind": "patent",
            "rec_number": 1,
            "title": "P",
            "volume": "1",
            "number": "US123",
            "pages": "9",
        }
    )
    assert {"volume", "issue", "page"}.isdisjoint(patent)
    book = zoterocite.csl_item(
        {"bib_type": "book", "rec_number": 1, "title": "B", "volume": "2", "pages": "9"}
    )
    assert book["volume"] == "2" and "page" not in book


def test_zotero_name_without_a_family_part_is_a_literal() -> None:
    item = zoterocite.csl_item(
        {
            "kind": "paper",
            "bib_type": "article",
            "rec_number": 9,
            "title": "T",
            "authors": [
                {"name": "Plato"},
                {"name": "Marie Curie"},
                {"name": ""},
                {"family": "Doe", "given": "J."},
            ],
        }
    )
    assert item["author"] == [
        {"literal": "Plato"},
        {"family": "Curie", "given": "Marie"},
        {"family": "Doe", "given": "J."},
    ]


def test_zotero_organisations_are_literals_through_the_shared_decision() -> None:
    """The same rows the Word source writes as ``b:Corporate``: a marked row,
    a datasheet's vendor in place of its byline, a software record's author
    that is its publisher. An unmarked name stays a person."""
    base: dict[str, Any] = {"kind": "paper", "bib_type": "report", "rec_number": 9}
    for author in (
        {"literal": "World Health Organization"},
        {"name": "{World Health Organization}"},
        {"name": "World Health Organization", "kind": "organization"},
    ):
        item = zoterocite.csl_item({**base, "title": "T", "authors": [author]})
        assert item["author"] == [{"literal": "World Health Organization"}], author
    unmarked = zoterocite.csl_item(
        {**base, "title": "T", "authors": [{"name": "World Health Organization"}]}
    )
    assert unmarked["author"] == [{"family": "Organization", "given": "World Health"}]
    sheet = zoterocite.csl_item(
        {
            "kind": "datasheet",
            "rec_number": 9,
            "title": "ESP32",
            "authors": [],
            "vendor": "Espressif Systems",
        }
    )
    assert sheet["author"] == [{"literal": "Espressif Systems"}]
    software = zoterocite.csl_item(
        {
            "kind": "paper",
            "bib_type": "software",
            "rec_number": 9,
            "title": "Tool",
            "authors": [{"name": "Zenodo"}],
            "publisher": "Zenodo",
        }
    )
    assert software["author"] == [{"literal": "Zenodo"}]


def test_zotero_json_keeps_non_ascii_and_is_compact() -> None:
    source = {
        "kind": "paper",
        "bib_type": "article",
        "rec_number": 9,
        "title": "Über Zeichen – x",
    }
    raw = zoterocite.citation_json("abcd1234", "[1–3]", [(source, False)])
    assert "Über Zeichen – x" in raw and "[1–3]" in raw
    assert ": " not in raw and ", " not in raw
    assert list(json.loads(raw)) == [
        "citationID",
        "properties",
        "citationItems",
        "schema",
    ]
    assert list(json.loads(raw)["citationItems"][0]) == ["id", "uris", "itemData"]


def test_zotero_item_uri_is_deterministic_per_record() -> None:
    assert zoterocite.item_uri(7) == zoterocite.item_uri(7) != zoterocite.item_uri(8)
    libraries = {zoterocite.item_uri(n).split("/")[5] for n in (7, 8)}
    assert len(libraries) == 1  # one library, two item keys


# ── marks ─────────────────────────────────────────────────────────────


def _three_ctx() -> Any:
    refs = {
        "a1": _ref(1, "a1"),
        "b2": _ref(2, "b2"),
        "c3": _ref(3, "c3"),
        "d4": _ref(4, "d4"),
    }
    return _ctx(_Store(refs))


def test_zotero_single_mark_is_one_flat_complex_field() -> None:
    ctx = _three_ctx()
    para = _render("See [§a1].", ctx)
    kinds = [
        e.tag.split("}")[1] + (e.get(f"{{{_W}}}fldCharType") or "")
        for e in para._p.iterfind("w:r/*", _NS)
        if e.tag != f"{{{_W}}}rPr"
    ]
    assert kinds == [
        "t",
        "fldCharbegin",
        "instrText",
        "fldCharseparate",
        "t",
        "fldCharend",
        "t",
    ]
    assert para._p.find("w:sdt", _NS) is None
    ((data, cached),) = _marks(para._p)
    assert cached == "[1]"
    assert data["citationItems"] == [
        {
            "id": 1,
            "uris": [zoterocite.item_uri(1)],
            "itemData": {
                "id": 1,
                "type": "article-journal",
                "title": "Title of a1",
                "author": [{"family": "Doe", "given": "J."}],
                "issued": {"date-parts": [[2024]]},
            },
        }
    ]
    # python-docx's own reading sees the mark like an EndNote field.
    assert para.text == "See [1]."
    instr = para._p.find(".//w:instrText", _NS)
    assert instr is not None
    assert instr.get("{http://www.w3.org/XML/1998/namespace}space") == "preserve"
    assert instr.text is not None and instr.text.startswith(_ITEM + '{"citationID":"')


def test_zotero_adjacent_cites_are_one_grouped_field() -> None:
    ctx = _three_ctx()
    para = _render("See [§a1][§b2][§c3] here.", ctx)
    ((data, cached),) = _marks(para._p)
    assert cached == "[1–3]"
    assert data["properties"]["formattedCitation"] == "[1–3]"
    assert [i["id"] for i in data["citationItems"]] == [1, 2, 3]
    assert para.text == "See [1–3] here."


def _si_ctx(monkeypatch: pytest.MonkeyPatch) -> Any:
    """``parent24si`` is the SI record of ``parent24``; ``other14`` a plain
    paper cited second."""
    from precis.store import si_links

    parent = _ref(1, "parent24")
    si = _ref(2, "parent24si")
    si.pdf_role = "supplement"
    other = _ref(3, "other14")

    class _Conn:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *a: object) -> None:
            return None

    store = _Store({"parent24": parent, "parent24si": si, "other14": other})
    store.pool = SimpleNamespace(connection=lambda: _Conn())
    monkeypatch.setattr(
        si_links,
        "supplement_parent",
        lambda _c, rid: (1, "parent24") if rid == 2 else None,
    )
    return _ctx(store)


def test_zotero_si_cite_carries_its_postnote_as_the_item_suffix(monkeypatch) -> None:
    ctx = _si_ctx(monkeypatch)
    para = _render("Data [§parent24si].", ctx)
    assert ctx.cited == ["parent24"]
    ((data, cached),) = _marks(para._p)
    assert cached == "[1] (SI)"
    assert data["properties"]["formattedCitation"] == "[1] (SI)"
    (item,) = data["citationItems"]
    assert item["id"] == 1 and item["suffix"] == " (SI)"


def test_zotero_si_next_to_a_plain_cite_stays_one_field(monkeypatch) -> None:
    """The suffix binds to its own item in the JSON, so a mixed run needs no
    second field: only the SI item carries it."""
    ctx = _si_ctx(monkeypatch)
    ctx.cite_number("parent24")
    ctx.cite_number("other14")
    para = _render("Data [§other14][§parent24si] here.", ctx)
    ((data, cached),) = _marks(para._p)
    assert cached == "[1 (SI), 2]"
    by_id = {i["id"]: i for i in data["citationItems"]}
    assert by_id[1]["suffix"] == " (SI)" and "suffix" not in by_id[3]
    assert para.text == "Data [1 (SI), 2] here."


def test_zotero_dropped_repeat_prints_no_second_mark_and_closes_up() -> None:
    ctx = _three_ctx()
    para = _render("First [§a1] then more [§a1] again.", ctx)
    assert len(_marks(para._p)) == 1  # the repeat is not written
    assert para.text == "First [1] then more again."
    # ... and the mark repeats when asked for, with its own id.
    ctx = _three_ctx()
    ctx.repeat_cites = True
    para = _render("First [§a1] then more [§a1] again.", ctx)
    marks = _marks(para._p)
    assert len(marks) == 2
    assert marks[0][0]["citationID"] != marks[1][0]["citationID"]


def test_zotero_same_doi_alias_is_one_source() -> None:
    store = _Store(
        {"a1": _ref(1, "a1"), "b2": _ref(2, "b2", year=2023)},
        {"a1": "10.1/X", "b2": "https://doi.org/10.1/x"},
    )
    ctx = _ctx(store)
    para = _render("Both [§a1] and [§b2].", ctx)
    assert ctx.cited == ["a1"]
    marks = _marks(para._p)
    assert [m[1] for m in marks] == ["[1]", "[1]"]
    assert [[i["id"] for i in m[0]["citationItems"]] for m in marks] == [
        [1],
        [1],
    ]  # both marks name the one record


def test_zotero_unresolved_slug_degrades_to_a_plain_marker() -> None:
    ctx = _ctx(_Store({"a1": _ref(1, "a1")}))
    para = _render("See [§a1][§nosuch9].", ctx)
    assert ctx.cited == ["a1", "nosuch9"]
    ((data, cached),) = _marks(para._p)
    assert cached == "[1]" and [i["id"] for i in data["citationItems"]] == [1]
    assert "[2]" in para.text
    assert any("nosuch9" in w for w in ctx.warnings)


def test_zotero_mode_leaves_no_notes_and_no_endnote_state() -> None:
    ctx = _three_ctx()
    para = _render("Quoted [pc10].", ctx)
    assert "research-notes" not in etree.tostring(para._p).decode()
    assert ctx._kept_field is None


def test_zotero_citation_ids_are_unique_lowercase_alphanumeric() -> None:
    zc = zoterocite.ZoteroCitations()
    ids = [zc.next_id([1, 2]) for _ in range(200)]
    assert len(set(ids)) == 200
    assert all(re.fullmatch(r"[a-z0-9]{8}", i) for i in ids)
    assert zoterocite.ZoteroCitations().next_id([1, 2]) == ids[0]  # deterministic


# ── preferences ───────────────────────────────────────────────────────


def test_zotero_preference_pieces_are_at_most_255_characters_and_reassemble() -> None:
    pieces = zoterocite.pref_pieces("abc12345")
    assert "".join(pieces) == zoterocite._PREFS % (
        "abc12345",
        "http://www.zotero.org/styles/ieee",
    )
    assert all(len(p) <= 255 for p in pieces)
    # The real string is just under one piece; a longer one splits at 255.
    data = "".join(chr(0x61 + i % 26) for i in range(600))
    long = zoterocite.split_pieces(data)
    assert [len(p) for p in long] == [255, 255, 90]
    assert "".join(long) == data
    blob = zoterocite.props_blob(long)
    root = etree.fromstring(blob)
    assert root.tag == f"{{{_CP}}}Properties"
    assert root.nsmap == {None: _CP, "vt": _VT}
    text, props = _pref(blob)
    assert text == data
    assert [p[:2] for p in props] == [
        ("ZOTERO_PREF_1", "2"),
        ("ZOTERO_PREF_2", "3"),
        ("ZOTERO_PREF_3", "4"),
    ]


def test_zotero_preferences_are_added_to_an_existing_custom_properties_part() -> None:
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part

    doc = docx.Document()
    existing = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Properties xmlns="{_CP}" xmlns:vt="{_VT}">'
        '<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="2" '
        'name="Other"><vt:lpwstr>keep</vt:lpwstr></property>'
        '<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="3" '
        'name="ZOTERO_PREF_1"><vt:lpwstr>old</vt:lpwstr></property>'
        "</Properties>"
    ).encode()
    package = doc.part.package
    part = Part(
        PackURI("/docProps/custom.xml"),
        zoterocite._CT_CUSTOM_PROPS,
        existing,
        package,
    )
    package.relate_to(part, zoterocite._RT_CUSTOM_PROPS)
    zoterocite.install_prefs(doc, "abc12345")
    zoterocite.install_prefs(doc, "abc12345")  # idempotent: replaced, not stacked
    parts = [
        p for p in package.iter_parts() if str(p.partname) == "/docProps/custom.xml"
    ]
    assert len(parts) == 1
    text, props = _pref(parts[0].blob)
    assert text.count("<data ") == 1 and "old" not in text
    root = etree.fromstring(parts[0].blob)
    assert root.findtext("cp:property/vt:lpwstr", namespaces=_NS) == "keep"
    assert [p[0] for p in props] == ["Other", "ZOTERO_PREF_1"]
    assert [p[1] for p in props] == ["2", "3"]  # pids continue after the other's


# ── the controls ──────────────────────────────────────────────────────


def test_zotero_is_an_implemented_mode() -> None:
    from precis.export import IMPLEMENTED_CITATION_MODES, guard_citation_mode

    assert IMPLEMENTED_CITATION_MODES == ("endnote", "word", "zotero", "plain")
    guard_citation_mode("zotero")

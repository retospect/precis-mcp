"""Word-native citations (``CITATION`` / ``BIBLIOGRAPHY`` fields) for the
.docx export.

Word keeps the sources of a document's bibliography in a *custom XML part*
(``/customXml/item1.xml``, a ``b:Sources`` tree) and cites them with
``CITATION`` fields; the reference list is a ``BIBLIOGRAPHY`` field. Both sit
in structured document tags (``w:sdt``) so Word treats a mark as one citation
content control and the list as one bibliography control. A co-author with
plain Word (no add-in) can update the numbers, switch the style and add
sources; the numbering stays live.

Everything here is hand-written OOXML, built from what Word documents about
its bibliography part. Round-trip fidelity is checked by reading the package
back, not by opening it in Word. The cached text of a mark and of the list is
the export's own (the same numbers every other mode prints); Word replaces it
on a field update.

Ids that must be unique (``b:Tag``, ``b:Guid``, the ``w:sdt`` ids) are
derived from the cite key or counted, never random, so an export is
byte-for-byte repeatable.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from precis.export.endnote import (
    _cached_run,
    _fld_char,
    _instr_text,
    _run,
    _sortable_name,
    _strip_markup,
)
from precis.utils.authors import author_display
from precis.utils.text import esc

_NS_BIB = "http://schemas.openxmlformats.org/officeDocument/2006/bibliography"
_NS_DS = "http://schemas.openxmlformats.org/officeDocument/2006/customXml"
_RT_CUSTOM_XML = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXml"
)
_RT_CUSTOM_XML_PROPS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXmlProps"
)
_CT_XML = "application/xml"
_CT_PROPS = "application/vnd.openxmlformats-officedocument.customXmlProperties+xml"

#: Word's own name for the numbering style the in-text marks and the list
#: follow; the cached text is IEEE-style ``[n]``.
_STYLE_NAME = "IEEE"
_SELECTED_STYLE = "\\IEEE2006OfficeOnline.xsl"
#: The locale switch of a ``CITATION`` field (``\l``): US English.
_LCID = 1033

#: Namespace for the derived GUIDs (a constant, so a tag always maps to the
#: same GUID).
_GUID_NS = uuid.UUID("5d0f3c1e-7a4b-4f4e-9d6b-2c1a8e3b7f10")

#: The entry type of a source (``endnote._REF_TYPES`` keys, the same decision
#: the reference line makes: ``record_bib_type`` / ``paper_bib_type``) to
#: Word's ``b:SourceType``. A thesis has no Word type of its own: ``Report``.
_SOURCE_TYPES: dict[str, str] = {
    "article": "JournalArticle",
    "incollection": "BookSection",
    "book": "Book",
    "inproceedings": "ConferenceProceedings",
    "report": "Report",
    "thesis": "Report",
    "online": "InternetSite",
    "patent": "Patent",
    "datasheet": "Misc",
    "software": "Misc",
    "dataset": "Misc",
}

#: The source types that carry a volume (and, but a whole book, pages).
_VOLUME_TYPES = frozenset(
    {"JournalArticle", "BookSection", "ConferenceProceedings", "Book"}
)

#: Characters XML 1.0 cannot carry.
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def source_type(source: dict[str, Any]) -> str:
    """Word's ``b:SourceType`` for a resolved source dict."""
    key = source.get("bib_type") or source.get("kind") or "article"
    return _SOURCE_TYPES.get(key, "JournalArticle")


def clean_text(value: Any) -> str:
    """A field value as plain text: markup and entities resolved, control
    characters XML cannot carry dropped (the Zotero items share it)."""
    return drop_illegal(_strip_markup(value)).strip()


def drop_illegal(text: str) -> str:
    """``text`` without the control characters XML 1.0 cannot carry."""
    return _XML_ILLEGAL.sub("", text)


def _text(value: Any) -> str:
    """A field value as XML text: :func:`clean_text`, escaped once."""
    return esc(clean_text(value))


def _guid(seed: str) -> str:
    return "{" + str(uuid.uuid5(_GUID_NS, seed)).upper() + "}"


#: Values of an author row's ``kind`` / ``type`` that mark an organisation.
_ORG_KINDS = frozenset(
    {"organization", "organisation", "corporate", "institution", "org"}
)
#: A name wholly in braces, BibTeX's way to keep a corporate name whole.
_BRACED = re.compile(r"\{([^{}]+)\}")


def _unbrace(name: str) -> str:
    braced = _BRACED.fullmatch(name)
    return braced.group(1).strip() if braced else name


def organisation_name(author: Any) -> str:
    """The name of an author row the record itself marks as an organisation
    or a literal name, ``""`` for anything else (a name is never judged by its
    spelling). A row is marked by a ``literal`` key, a ``kind`` / ``type`` of
    :data:`_ORG_KINDS`, or a name wholly in braces (``{World Health
    Organization}``; the braces are dropped). The Word and Zotero sources
    both decide through this."""
    flag = ""
    if isinstance(author, dict):
        literal = clean_text(author.get("literal"))
        if literal:
            return _unbrace(literal)
        flag = str(author.get("kind") or author.get("type") or "").strip().lower()
    name = clean_text(author_display(author))
    if flag in _ORG_KINDS:
        return _unbrace(name)
    braced = _BRACED.fullmatch(name)
    return braced.group(1).strip() if braced else ""


def source_authors(source: dict[str, Any]) -> list[tuple[bool, str]]:
    """The authors of a resolved source as ``(is_organisation, name)``, the
    name clean text (a person as ``Family, Given``). An organisation is a row
    :func:`organisation_name` knows, a datasheet's vendor standing in for the
    byline it has none of, and, on a software or dataset record, an author
    that is the record's own publisher."""
    publisher = clean_text(source.get("publisher")).casefold()
    whole_record = source.get("bib_type") in ("software", "dataset")
    out: list[tuple[bool, str]] = []
    for a in source.get("authors") or []:
        org = organisation_name(a)
        if org:
            out.append((True, org))
        elif name := clean_text(_sortable_name(a)):
            out.append((whole_record and name.casefold() == publisher, name))
    vendor = clean_text(source.get("vendor"))
    if source.get("kind") == "datasheet" and vendor and not out:
        out.append((True, vendor))
    return out


def _persons(source: dict[str, Any], role: str = "Author") -> str:
    """The ``b:Author`` element, its ``role`` child (``Author``; ``Inventor``
    on a patent) holding one ``b:Person`` per author, ``Family, Given`` split
    at the first comma (a name without one is the last name). A lone
    organisation is ``b:Corporate``; beside persons it is a ``b:Person`` with
    only ``b:Last``, as the name list and ``b:Corporate`` exclude each
    other."""
    names = source_authors(source)
    if not names:
        return ""
    if len(names) == 1 and names[0][0]:
        inner = f"<b:Corporate>{esc(names[0][1])}</b:Corporate>"
    else:
        people = []
        for is_org, name in names:
            if is_org:
                last, first = name, ""
            else:
                last, _, first = name.partition(",")
            person = f"<b:Last>{esc(last.strip())}</b:Last>"
            if first.strip():
                person += f"<b:First>{esc(first.strip())}</b:First>"
            people.append(f"<b:Person>{person}</b:Person>")
        inner = "<b:NameList>" + "".join(people) + "</b:NameList>"
    return f"<b:Author><b:{role}>{inner}</b:{role}></b:Author>"


def source_xml(source: dict[str, Any], tag: str, ref_order: int) -> str:
    """One ``<b:Source>`` for a resolved source dict (``kind``, ``bib_type``,
    ``authors``, ``title``, ``year``, ``journal`` — the venue —, ``volume``,
    ``number``, ``pages``, ``publisher``, ``institution``, ``vendor``,
    ``patent_number``, ``doi``, ``url``). An element whose value is empty is
    left out."""
    stype = source_type(source)
    venue = source.get("journal") or source.get("container_title")
    publisher = source.get("publisher")
    fields: list[tuple[str, Any]] = [("Title", source.get("title"))]
    if stype == "JournalArticle":
        fields.append(("JournalName", venue))
    elif stype == "BookSection":
        fields.append(("BookTitle", venue))
    elif stype == "ConferenceProceedings":
        fields.append(("ConferenceName", venue))
    fields.append(("Year", source.get("year")))
    if stype == "Patent":
        fields.append(("PatentNumber", source.get("patent_number")))
    # Volume, issue and pages only where the type has them: a patent's or a
    # report's "number" is not an issue.
    if stype in _VOLUME_TYPES:
        fields.append(("Volume", source.get("volume")))
    if stype == "JournalArticle":
        fields.append(("Issue", source.get("number")))
    if stype in _VOLUME_TYPES and stype != "Book":
        fields.append(("Pages", source.get("pages")))
    if stype in ("Book", "BookSection", "ConferenceProceedings", "Misc"):
        fields.append(("Publisher", publisher))
    if stype == "Report":
        fields.append(("Institution", source.get("institution") or publisher))
    if stype == "InternetSite":
        fields.append(("URL", source.get("url")))
    fields.append(("DOI", source.get("doi")))
    body = "".join(
        f"<b:{name}>{text}</b:{name}>"
        for name, value in fields
        if (text := _text(value))
    )
    people = _persons(source, "Inventor" if stype == "Patent" else "Author")
    return (
        f"<b:Source><b:Tag>{tag}</b:Tag><b:SourceType>{stype}</b:SourceType>"
        f"<b:Guid>{_guid(tag)}</b:Guid>{people}{body}"
        f"<b:RefOrder>{ref_order}</b:RefOrder></b:Source>"
    )


def sources_xml(entries: list[tuple[str, int, dict[str, Any]]]) -> bytes:
    """The ``/customXml/item1.xml`` blob: ``b:Sources`` over one source per
    ``(tag, reference number, source)`` entry."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        f'<b:Sources xmlns:b="{_NS_BIB}" xmlns="{_NS_BIB}" '
        f'SelectedStyle="{esc(_SELECTED_STYLE, quote=True)}" '
        f'StyleName="{_STYLE_NAME}" Version="2006">'
        + "".join(source_xml(src, tag, n) for tag, n, src in entries)
        + "</b:Sources>"
    )
    return xml.encode("utf-8")


def props_xml(seed: str) -> bytes:
    """The ``/customXml/itemProps1.xml`` blob: the datastore item that
    declares ``item1.xml`` a bibliography-schema part."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        f'<ds:datastoreItem ds:itemID="{_guid(seed)}" xmlns:ds="{_NS_DS}">'
        f'<ds:schemaRefs><ds:schemaRef ds:uri="{_NS_BIB}"/></ds:schemaRefs>'
        "</ds:datastoreItem>"
    )
    return xml.encode("utf-8")


def _existing_sources_part(doc: Any) -> Any:
    """The document's own bibliography sources part, if it has one (python-docx's
    template ships an empty ``b:Sources`` as ``/customXml/item1.xml``): Word
    reads one, so it is filled, not added beside."""
    for rel in doc.part.rels.values():
        if rel.is_external or not rel.reltype.endswith("/customXml"):
            continue
        blob = rel.target_part.blob
        if b"<b:Sources" in blob and _NS_BIB.encode() in blob:
            return rel.target_part
    return None


def install_sources(doc: Any, entries: list[tuple[str, int, dict[str, Any]]]) -> None:
    """Store the sources in the package: ``/customXml/item1.xml`` related from
    the document part, ``itemProps1.xml`` related from ``item1.xml``. A
    bibliography part the document already carries (the template's empty one)
    gets the sources and the props; otherwise the next free ``itemN`` pair is
    added."""
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part

    blob = sources_xml(entries)
    props_blob = props_xml("precis-bibliography")
    item = _existing_sources_part(doc)
    if item is not None:
        item._blob = blob
        for rel in item.rels.values():
            if not rel.is_external and rel.reltype == _RT_CUSTOM_XML_PROPS:
                rel.target_part._blob = props_blob
                return
        props = Part(
            PackURI(str(item.partname).replace("/item", "/itemProps")),
            _CT_PROPS,
            props_blob,
            item.package,
        )
        item.relate_to(props, _RT_CUSTOM_XML_PROPS)
        return
    package = doc.part.package
    taken = {str(p.partname) for p in package.iter_parts()}
    n = 1
    while f"/customXml/item{n}.xml" in taken or f"/customXml/itemProps{n}.xml" in taken:
        n += 1
    item = Part(PackURI(f"/customXml/item{n}.xml"), _CT_XML, blob, package)
    props = Part(
        PackURI(f"/customXml/itemProps{n}.xml"), _CT_PROPS, props_blob, package
    )
    item.relate_to(props, _RT_CUSTOM_XML_PROPS)
    doc.part.relate_to(item, _RT_CUSTOM_XML)


class WordCitations:
    """Per-export state: the ``b:Tag`` of each cited slug (unique, letters
    and digits only, derived from the slug) and the running ``w:sdt`` id."""

    def __init__(self) -> None:
        self._tags: dict[str, str] = {}
        self._used: set[str] = set()
        self._sdt_id = 0

    def tag(self, slug: str) -> str:
        """The unique tag of ``slug``: its letters and digits (``Src`` when
        none; an ``R`` in front of a leading digit, as a tag starts with a
        letter), a counter appended on a clash."""
        tag = self._tags.get(slug)
        if tag is None:
            base = re.sub(r"[^A-Za-z0-9]", "", slug) or "Src"
            if base[0].isdigit():
                base = "R" + base
            tag, i = base, 1
            while tag in self._used:
                i += 1
                tag = f"{base}{i}"
            self._tags[slug] = tag
            self._used.add(tag)
        return tag

    def next_id(self) -> int:
        self._sdt_id += 1
        return self._sdt_id


def _sdt(kind: str, sdt_id: int) -> tuple[Any, Any]:
    """An empty ``w:sdt`` (``kind``: ``citation`` / ``bibliography``) and its
    ``w:sdtContent``."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    sdt = OxmlElement("w:sdt")
    pr = OxmlElement("w:sdtPr")
    ident = OxmlElement("w:id")
    ident.set(qn("w:val"), str(sdt_id))
    pr.append(ident)
    pr.append(OxmlElement(f"w:{kind}"))
    content = OxmlElement("w:sdtContent")
    sdt.append(pr)
    sdt.append(content)
    return sdt, content


def citation_instr(tags: list[str], suffixes: list[str] | None = None) -> str:
    """`` CITATION a \\s " (SI)" \\m b \\l 1033 `` for ``tags``: the first,
    then the further sources each behind ``\\m``. ``suffixes`` (one per tag)
    puts a postnote switch (``\\s``) directly after the tag of the source
    it belongs to; the locale switch comes last."""
    out = " CITATION"
    for i, tag in enumerate(tags):
        out += f" \\m {tag}" if i else f" {tag}"
        suffix = suffixes[i] if suffixes else ""
        if suffix:
            out += f' \\s "{suffix}"'
    return out + f" \\l {_LCID} "


def add_citation(
    paragraph: Any,
    wc: WordCitations,
    tags: list[str],
    cached: str,
    suffixes: list[str] | None = None,
) -> None:
    """Append one citation mark to ``paragraph``: a ``w:sdt`` holding ONE
    complex ``CITATION`` field whose cached result is ``cached``."""
    sdt, content = _sdt("citation", wc.next_id())
    content.append(_fld_char("begin"))
    content.append(_instr_text(citation_instr(tags, suffixes)))
    content.append(_fld_char("separate"))
    content.append(_cached_run(cached))
    content.append(_fld_char("end"))
    paragraph._p.append(sdt)


def add_bibliography(
    doc: Any, wc: WordCitations, cached_lines: list[tuple[str, str]], style: Any = None
) -> None:
    """Append the reference list: one ``w:sdt`` (``w:bibliography``) around
    one paragraph per ``(label, line)`` in ``style`` (label, a tab, the line),
    the first opening a ``BIBLIOGRAPHY`` complex field and the last closing
    it."""
    from docx.oxml import OxmlElement

    paras = [doc.add_paragraph(style=style) for _ in cached_lines or [("", "")]]
    sdt, content = _sdt("bibliography", wc.next_id())
    paras[0]._p.addprevious(sdt)
    for para in paras:
        content.append(para._p)  # moves it from the body into the control
    first = paras[0]._p
    first.append(_fld_char("begin"))
    first.append(_instr_text(" BIBLIOGRAPHY "))
    first.append(_fld_char("separate"))
    for para, (label, line) in zip(paras, cached_lines, strict=False):
        p = para._p
        p.append(_cached_run(label))
        p.append(_run(OxmlElement("w:tab")))
        p.append(_cached_run(line))
    paras[-1]._p.append(_fld_char("end"))

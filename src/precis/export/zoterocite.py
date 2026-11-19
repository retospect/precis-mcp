"""Zotero citation fields for the .docx export.

The Zotero word-processor plugin keeps a citation as one complex field,
``ADDIN ZOTERO_ITEM CSL_CITATION {json}``, whose JSON carries each cited item
as embedded CSL JSON (``itemData``), and the reference list as one field
``ADDIN ZOTERO_BIBL {json} CSL_BIBLIOGRAPHY``. The plugin recognises a file by
document preferences kept as custom document properties
(``ZOTERO_PREF_1`` ...). A co-author with the plugin can keep citing and can
refresh the numbers and the list; without it the cached ``[n]`` text shows.

Everything here is hand-written, built from what the plugin documents about
its field format. Round-trip fidelity is checked by reading the package back,
not by opening it in Word. The cached text of a mark and of the list is the
export's own (the same numbers every other mode prints); the plugin replaces it
on a refresh.

Every id (the citation ids, the item keys, the session) is derived from the
cited records, never random, so an export is byte-for-byte repeatable. The
Zotero library the item URIs name is a placeholder: the plugin falls back to
the embedded ``itemData`` when the URI is not in the reader's library.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from precis.export.endnote import _cached_run, _fld_char, _instr_text
from precis.export.wordcite import clean_text, drop_illegal, source_authors

#: Item keys: eight upper-case characters from Zotero's own key alphabet.
_ITEM_ALPHABET = "23456789ABCDEFGHIJKLMNPQRSTUVWXYZ"
_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

#: The placeholder local library every item URI names (a constant).
_LIBRARY_SEED = "precis-export-library"

_SCHEMA = (
    "https://github.com/citation-style-language/schema/raw/master/csl-citation.json"
)
_ITEM_INSTR = " ADDIN ZOTERO_ITEM CSL_CITATION "
#: The reference-list field's instruction (the preference-less defaults).
BIBL_INSTR = (
    ' ADDIN ZOTERO_BIBL {"uncited":[],"omitted":[],"custom":[]} CSL_BIBLIOGRAPHY '
)

_NS_CP = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
_NS_VT = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
_RT_CUSTOM_PROPS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
    "custom-properties"
)
_CT_CUSTOM_PROPS = "application/vnd.openxmlformats-officedocument.custom-properties+xml"
_PART_NAME = "/docProps/custom.xml"
_FMTID = "{D5CDD505-2E9C-101B-9397-08002B2CF9AE}"
_PREF_PREFIX = "ZOTERO_PREF_"
#: One property value holds at most this many characters.
_PREF_CHUNK = 255

#: Canonical biblatex style (:data:`precis.export.latex._BIB_STYLES` value) →
#: the Zotero style the document preferences name, so a refresh formats in the
#: draft's own family (EndNote mode: ``endnote.endnote_style``). Anything
#: else is IEEE, the numeric default.
_STYLE_IDS: dict[str, str] = {
    "chem-acs": "american-chemical-society",
    "chem-rsc": "royal-society-of-chemistry",
    "nature": "nature",
}
_STYLE_URL = "http://www.zotero.org/styles/"

#: The document preferences the plugin reads, ``%s`` the session id and the
#: style id.
_PREFS = (
    '<data data-version="3" zotero-version="6.0.30"><session id="%s"/>'
    '<style id="%s" locale="en-US" '
    'hasBibliography="1" bibliographyStyleHasBeenSet="0"/>'
    '<prefs><pref name="fieldType" value="Field"/></prefs></data>'
)


def zotero_style(bib_style: str) -> str:
    """The Zotero style id (a URL) for a canonical biblatex ``bib_style``
    (already through :func:`precis.export.latex.resolve_bib_style`)."""
    return _STYLE_URL + _STYLE_IDS.get(bib_style, "ieee")


#: The entry type of a source (``endnote._REF_TYPES`` keys, the same decision
#: the reference line makes: ``record_bib_type`` / ``paper_bib_type``) to a CSL
#: item type.
_CSL_TYPES: dict[str, str] = {
    "article": "article-journal",
    "incollection": "chapter",
    "book": "book",
    "inproceedings": "paper-conference",
    "report": "report",
    "thesis": "thesis",
    "online": "webpage",
    "patent": "patent",
    "datasheet": "document",
    "software": "software",
    "dataset": "dataset",
}

#: Types that carry a volume; a journal article also an issue; all but a whole
#: book pages (a patent's or a report's "number" is not an issue).
_VOLUME_TYPES = frozenset({"article-journal", "chapter", "paper-conference", "book"})
#: Types whose venue is the item's container (journal, book, proceedings,
#: web site).
_CONTAINER_TYPES = frozenset(
    {"article-journal", "chapter", "paper-conference", "webpage"}
)
#: Types that carry a publisher (a report's or thesis's is its institution).
_PUBLISHER_TYPES = frozenset(
    {
        "book",
        "chapter",
        "paper-conference",
        "report",
        "thesis",
        "document",
        "software",
        "dataset",
    }
)


def csl_type(source: dict[str, Any]) -> str:
    """The CSL item type for a resolved source dict."""
    key = source.get("bib_type") or source.get("kind") or "article"
    return _CSL_TYPES.get(key, "article-journal")


def _key(seed: str, alphabet: str, length: int = 8) -> str:
    """``length`` characters of ``alphabet`` derived from ``seed`` (a hash, so
    the same seed always gives the same key)."""
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return "".join(alphabet[b % len(alphabet)] for b in digest[:length])


def item_uri(rec_number: Any) -> str:
    """The local-library URI of a record: the library key from a constant, the
    item key from the record id."""
    library = _key(_LIBRARY_SEED, _ID_ALPHABET)
    item = _key(f"item:{rec_number}", _ITEM_ALPHABET)
    return f"http://zotero.org/users/local/{library}/items/{item}"


def _authors(source: dict[str, Any]) -> list[dict[str, str]]:
    """CSL names: ``Family, Given`` split at the first comma; a name that
    cannot be split, and an organisation (decided by
    :func:`precis.export.wordcite.source_authors`, as for Word), is a
    ``literal``."""
    out = []
    for is_org, name in source_authors(source):
        family, _, given = name.partition(",")
        if not is_org and family.strip() and given.strip():
            out.append({"family": family.strip(), "given": given.strip()})
        else:
            out.append({"literal": name if is_org else name.strip(" ,") or name})
    return out


#: A run style (``docx._BIB_TAG_STYLE`` values) → the opening and closing
#: tag of Zotero's inline markup for it, outermost first.
_CSL_TAGS: tuple[tuple[str, str, str], ...] = (
    ("i", "<i>", "</i>"),
    ("b", "<b>", "</b>"),
    ("sub", "<sub>", "</sub>"),
    ("sup", "<sup>", "</sup>"),
    ("sc", '<span style="font-variant:small-caps;">', "</span>"),
)


def csl_markup(runs: list[tuple[str, frozenset[str]]]) -> str:
    """A title or venue given as styled runs (``(text, styles)``, styles among
    ``i``, ``b``, ``sub``, ``sup``, ``sc``) as the string a CSL field takes:
    each styled run wrapped in Zotero's inline tag, nothing else escaped.
    Neighbouring runs of one style share their tags."""
    merged: list[tuple[str, frozenset[str]]] = []
    for text, styles in runs:
        if merged and merged[-1][1] == styles:
            merged[-1] = (merged[-1][0] + text, styles)
        else:
            merged.append((text, styles))
    out = []
    for text, styles in merged:
        opening = "".join(o for st, o, _c in _CSL_TAGS if st in styles)
        closing = "".join(c for st, _o, c in reversed(_CSL_TAGS) if st in styles)
        out.append(f"{opening}{text}{closing}")
    return drop_illegal("".join(out)).strip()


def csl_item(source: dict[str, Any]) -> dict[str, Any]:
    """The CSL JSON item of a resolved source dict (``kind``, ``bib_type``,
    ``rec_number``, ``authors``, ``title``, ``year``, ``journal`` — the venue
    —, ``volume``, ``number``, ``pages``, ``publisher``, ``institution``,
    ``vendor``, ``doi``, ``url``; ``title_csl`` / ``venue_csl``, the title
    and venue with their markup as :func:`csl_markup` wrote it, stand in for
    the plain ones). A member whose value is empty is left out; the keys come
    in a fixed order. ``URL`` is left out beside a ``DOI``: a style would
    print both."""
    ctype = csl_type(source)
    venue = source.get("journal") or source.get("container_title")
    publisher = source.get("publisher")
    if ctype in ("report", "thesis"):
        publisher = source.get("institution") or publisher
    year = re.fullmatch(r"\d{1,4}", str(source.get("year") or "").strip())
    doi = clean_text(source.get("doi"))
    members: list[tuple[str, Any]] = [
        ("id", source["rec_number"]),
        ("type", ctype),
        ("title", source.get("title_csl") or clean_text(source.get("title"))),
        (
            "container-title",
            (source.get("venue_csl") or clean_text(venue))
            if ctype in _CONTAINER_TYPES
            else "",
        ),
        ("author", _authors(source)),
        ("issued", {"date-parts": [[int(year.group(0))]]} if year else None),
        ("volume", clean_text(source.get("volume")) if ctype in _VOLUME_TYPES else ""),
        (
            "issue",
            clean_text(source.get("number")) if ctype == "article-journal" else "",
        ),
        (
            "page",
            clean_text(source.get("pages"))
            if ctype in _VOLUME_TYPES and ctype != "book"
            else "",
        ),
        (
            "publisher",
            clean_text(publisher) if ctype in _PUBLISHER_TYPES else "",
        ),
        ("DOI", doi),
        ("URL", "" if doi else clean_text(source.get("url"))),
    ]
    return {k: v for k, v in members if v or v == 0}


def citation_json(
    citation_id: str, mark: str, items: list[tuple[dict[str, Any], bool]]
) -> str:
    """The ``CSL_CITATION`` JSON of one mark: ``items`` are ``(source,
    is_si)``; an SI item carries the ``" (SI)"`` postnote as its suffix."""
    cited = []
    for source, is_si in items:
        item: dict[str, Any] = {
            "id": source["rec_number"],
            "uris": [item_uri(source["rec_number"])],
            "itemData": csl_item(source),
        }
        if is_si:
            item["suffix"] = " (SI)"
        cited.append(item)
    obj = {
        "citationID": citation_id,
        "properties": {
            "formattedCitation": mark,
            "plainCitation": mark,
            "noteIndex": 0,
        },
        "citationItems": cited,
        "schema": _SCHEMA,
    }
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class ZoteroCitations:
    """Per-export state: the citation ids handed out (unique, derived from
    the mark's position and its records)."""

    def __init__(self) -> None:
        self._used: set[str] = set()
        self._marks = 0

    def next_id(self, rec_numbers: list[Any]) -> str:
        """The 8-character ``a-z0-9`` id of the next mark."""
        self._marks += 1
        seed = f"mark:{self._marks}:" + ",".join(str(n) for n in rec_numbers)
        cid, salt = _key(seed, _ID_ALPHABET), 0
        while cid in self._used:  # a hash clash: re-derive
            salt += 1
            cid = _key(f"{seed}:{salt}", _ID_ALPHABET)
        self._used.add(cid)
        return cid

    @staticmethod
    def session(cited: list[str]) -> str:
        """The 8-character session id of the document's preferences."""
        return _key("session:" + ",".join(cited), _ID_ALPHABET)


def add_citation(
    paragraph: Any,
    zc: ZoteroCitations,
    items: list[tuple[dict[str, Any], int, bool]],
    cached: str,
) -> None:
    """Append one citation mark to ``paragraph``: ONE complex field (no
    content control) whose instruction is ``ADDIN ZOTERO_ITEM CSL_CITATION``
    and its JSON, cached result ``cached``. ``items`` are ``(source,
    reference number, is_si)`` in print order."""
    cid = zc.next_id([source["rec_number"] for source, _n, _si in items])
    payload = citation_json(cid, cached, [(s, si) for s, _n, si in items])
    p = paragraph._p
    p.append(_fld_char("begin"))
    p.append(_instr_text(_ITEM_INSTR + payload + " "))
    p.append(_fld_char("separate"))
    p.append(_cached_run(cached))
    p.append(_fld_char("end"))


# ── document preferences (docProps/custom.xml) ────────────────────────


def split_pieces(data: str) -> list[str]:
    """``data`` as consecutive pieces of at most 255 characters (one custom
    property each)."""
    return [data[i : i + _PREF_CHUNK] for i in range(0, len(data), _PREF_CHUNK)]


def pref_pieces(session_id: str, bib_style: str = "") -> list[str]:
    """The document preference string, split for the custom properties."""
    return split_pieces(_PREFS % (session_id, zotero_style(bib_style)))


def props_blob(pieces: list[str], existing: bytes | None = None) -> bytes:
    """The ``/docProps/custom.xml`` blob: one ``ZOTERO_PREF_n`` property per
    piece, ``pid`` counting up from 2. Added to ``existing`` custom
    properties when there are some (an older ``ZOTERO_PREF_*`` is replaced)."""
    from lxml import etree

    cp = f"{{{_NS_CP}}}"
    if existing:
        root = etree.fromstring(existing)
        for prop in root.findall(f"{cp}property"):
            if (prop.get("name") or "").startswith(_PREF_PREFIX):
                root.remove(prop)
        pids = [int(p.get("pid") or 0) for p in root.findall(f"{cp}property")]
        pid = max([1, *pids]) + 1
    else:
        root = etree.fromstring(f'<Properties xmlns="{_NS_CP}" xmlns:vt="{_NS_VT}"/>')
        pid = 2
    for i, piece in enumerate(pieces, start=1):
        prop = etree.SubElement(root, f"{cp}property")
        prop.set("fmtid", _FMTID)
        prop.set("pid", str(pid + i - 1))
        prop.set("name", f"{_PREF_PREFIX}{i}")
        etree.SubElement(prop, f"{{{_NS_VT}}}lpwstr").text = piece
    return bytes(
        etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    )


def install_prefs(doc: Any, session_id: str, bib_style: str = "") -> None:
    """Store the document preferences in the package: the custom document
    properties part, related from the package root. A part the document
    already carries gets the properties added."""
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part

    package = doc.part.package
    pieces = pref_pieces(session_id, bib_style)
    for rel in package.rels.values():
        if not rel.is_external and rel.reltype == _RT_CUSTOM_PROPS:
            part = rel.target_part
            part._blob = props_blob(pieces, part.blob)
            return
    part = Part(PackURI(_PART_NAME), _CT_CUSTOM_PROPS, props_blob(pieces), package)
    package.relate_to(part, _RT_CUSTOM_PROPS)

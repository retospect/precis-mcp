"""EndNote *Cite While You Write* (CWYW) field emission for the .docx export.

Emits the proprietary ``ADDIN EN.CITE`` in-text citation fields + an
``ADDIN EN.REFLIST`` bibliography field, plus the ``EN.InstantFormat`` /
``EN.Layout`` / ``EN.Libraries`` document variables, so EndNote's CWYW
recognizes the citations and can reformat / manage them. Each in-text field
carries the **full** ``<record>`` — EndNote's "traveling library" — so the
cited papers need not already exist in the reader's EndNote library.

There is **no public spec** for this format: it was reverse-engineered
byte-for-byte from a real EndNote-authored ``.docx`` (see the module tests,
which pin the emitted shape against that ground truth). Round-trip fidelity
therefore tracks the EndNote version that authored the reference sample; the
cached in-text display + the cached bibliography are placeholders that
EndNote **regenerates** on "Update Citations and Bibliography", so their
exact styling does not matter — only the ``<record>`` data does.

Escaping note: the whole ``<EndNote>…</EndNote>`` payload is placed in a
``w:instrText`` *text node*, which python-docx/lxml XML-escapes once on
serialize and Word un-escapes once on read — the two cancel. So the payload
string built here must be **exactly** the XML EndNote should parse:
structural tags are literal ``<``/``>``; only the text *values* (title,
authors, …) are XML-escaped (via :func:`_esc`) so the reconstructed payload
is itself well-formed.
"""

from __future__ import annotations

import re
from typing import Any

from precis.export._cite_ids import merged_cite_numbers
from precis.utils.authors import author_display
from precis.utils.text import esc

#: EndNote ``ref-type`` codes (name, numeric code), verified against a real
#: EndNote-authored field (Journal Article = 17) and the standard EndNote
#: XML type list. A paper maps by its biblatex ``entry_type``
#: (:func:`precis.export.latex.paper_bib_type`); ``patent`` / ``datasheet``
#: kinds map directly. Anything unlisted keeps the journal default.
_REF_TYPES: dict[str, tuple[str, int]] = {
    "article": ("Journal Article", 17),
    "book": ("Book", 6),
    "incollection": ("Book Section", 5),
    "inproceedings": ("Conference Proceedings", 10),
    "report": ("Report", 27),
    "thesis": ("Thesis", 32),
    "online": ("Electronic Article", 43),
    "patent": ("Patent", 25),
    "datasheet": ("Generic", 13),
}

#: Canonical biblatex style (:data:`precis.export.latex._BIB_STYLES` value)
#: → the EndNote output-style name written into ``EN.Layout``, so
#: "Update Citations and Bibliography" reformats in the same family.
#: Names are EndNote builtins; every supported bib style is numeric.
_ENDNOTE_STYLES: dict[str, str] = {
    "numeric-comp": "Numbered",
    "chem-rsc": "Numbered",
    "chem-acs": "ACS",
    "nature": "Nature",
}


def endnote_style(bib_style: str) -> str:
    """The EndNote output-style name for a canonical biblatex ``bib_style``
    (already through :func:`precis.export.latex.resolve_bib_style`)."""
    return _ENDNOTE_STYLES.get(bib_style, "Numbered")


#: A single synthetic "traveling library" id shared by every emitted record.
#: EndNote keys a source on ``(db-id, rec-number)`` but formats from the
#: embedded ``<record>`` when the id isn't a live local library — which is
#: always our case, so a stable placeholder is correct and keeps exports
#: deterministic.
_DB_ID = "precismcp0traveling0library000000000"
_TIMESTAMP = "1700000000"  # fixed → deterministic export bytes


def _sortable_name(a: Any) -> str:
    """One author as ``Family, Given`` — EndNote's canonical parse order.
    A bare comma-less ``{"name": "Given Family"}`` is reordered on a
    last-token-family heuristic so one export doesn't mix name orders
    (stored ``Family, Given`` names and single-token names pass through)."""
    name = author_display(a, order="sortable")
    if name and "," not in name and " " in name:
        rest, last = name.rsplit(" ", 1)
        name = f"{last}, {rest}"
    return name


def _author_list(authors: list[dict[str, Any]] | None) -> list[str]:
    """Each author as EndNote's ``<author>`` string — ``Family, Given``
    via :func:`_sortable_name`."""
    return [name for a in authors or [] if (name := _sortable_name(a))]


def _first_family(authors: list[dict[str, Any]] | None) -> str:
    """The lead author's family name (best effort) for the ``<Author>``
    stub — ``_sortable_name``'s ``Family, Given`` makes the family the
    head; a single-token name is returned whole."""
    for a in authors or []:
        if isinstance(a, dict) and (a.get("family") or "").strip():
            return str(a["family"]).strip()
        disp = _sortable_name(a)
        if disp:
            return disp.split(",")[0].strip() or disp
    return ""


#: cap on an embedded cited-passage note so one field's XML stays sane.
_NOTE_MAX_CHARS = 4000


#: Crossref/ingest markup embedded in titles — ``C<sub>20</sub>`` — stripped
#: to text for the record (the field data would print the literal tags).
_TAG = re.compile(r"<[A-Za-z][^>]*>|</[A-Za-z][^>]*>")


def _strip_markup(text: Any) -> str:
    return _TAG.sub("", str(text or ""))


def build_record(source: dict[str, Any], notes: str | None = None) -> str:
    """One EndNote ``<record>…</record>`` from a resolved source dict
    (``kind``, ``tag``, ``rec_number`` = the corpus ref id, ``bib_type``,
    ``authors``, ``title``, ``year``, ``journal``, ``volume``, ``number``,
    ``pages``, ``doi``, ``url``). Absent fields are omitted.

    ``notes`` (the cited passage from a ``pc<id>`` chunk citation) is embedded
    as ``<research-notes>`` — EndNote's *Research Notes* field — so the author
    sees the exact cited text on the citation. (Caveat: EndNote drops
    Research Notes when a traveling library is imported into a real library;
    the passage is present in the field data regardless.)"""
    name, code = _REF_TYPES.get(
        source.get("bib_type") or source.get("kind") or "article",
        _REF_TYPES["article"],
    )
    # Same venue precedence as the .bib/.docx exporters.
    venue = _strip_markup(
        source.get("venue") or source.get("journal") or source.get("container_title")
    )
    rec = source["rec_number"]
    parts = [
        "<record>",
        f"<rec-number>{rec}</rec-number>",
        f'<foreign-keys><key app="EN" db-id="{_DB_ID}" '
        f'timestamp="{_TIMESTAMP}">{rec}</key></foreign-keys>',
        f'<ref-type name="{esc(name)}">{code}</ref-type>',
    ]
    authors = _author_list(source.get("authors"))
    if authors:
        inner = "".join(f"<author>{esc(a)}</author>" for a in authors)
        parts.append(f"<contributors><authors>{inner}</authors></contributors>")
    titles = [f"<title>{esc(_strip_markup(source.get('title')))}</title>"]
    if venue:
        titles.append(f"<secondary-title>{esc(venue)}</secondary-title>")
    parts.append("<titles>" + "".join(titles) + "</titles>")
    if venue:
        parts.append(f"<periodical><full-title>{esc(venue)}</full-title></periodical>")
    if source.get("pages"):
        parts.append(f"<pages>{esc(source['pages'])}</pages>")
    if source.get("volume"):
        parts.append(f"<volume>{esc(source['volume'])}</volume>")
    if source.get("number"):
        parts.append(f"<number>{esc(source['number'])}</number>")
    if source.get("year"):
        parts.append(f"<dates><year>{esc(source['year'])}</year></dates>")
    if source.get("doi"):
        # EndNote stores the DOI in <electronic-resource-num>.
        parts.append(
            f"<electronic-resource-num>{esc(source['doi'])}</electronic-resource-num>"
        )
    if source.get("url"):
        parts.append(
            f"<urls><related-urls><url>{esc(source['url'])}</url></related-urls></urls>"
        )
    else:
        parts.append("<urls></urls>")
    if notes:
        text = notes.strip()
        if len(text) > _NOTE_MAX_CHARS:
            text = text[:_NOTE_MAX_CHARS].rstrip() + "…"
        parts.append(f"<research-notes>{esc(text)}</research-notes>")
    parts.append("</record>")
    return "".join(parts)


def _cite_element(source: dict[str, Any], notes: str | None, cite_number: int) -> str:
    """One ``<Cite>…</Cite>`` element — author/year/RecNum stub + the full
    traveling-library ``<record>`` for ``source``. ``notes`` is the cited
    passage (``pc<id>`` chunk citation) → the record's
    ``<research-notes>``. ``cite_number`` is the 1-based citation number —
    the cached ``<DisplayText>`` shows ``[n]``, matching the numbered
    bibliography the export always emits."""
    first_family = _first_family(source.get("authors"))
    year = source.get("year")
    rec = source["rec_number"]
    return (
        "<Cite>"
        f"<Author>{esc(first_family)}</Author>"
        f"<Year>{esc(year) if year else ''}</Year>"
        f"<RecNum>{rec}</RecNum>"
        f"<DisplayText>[{cite_number}]</DisplayText>"
        f"{build_record(source, notes)}"
        "</Cite>"
    )


def citation_payload(
    source: dict[str, Any], notes: str | None = None, cite_number: int = 1
) -> str:
    """The full ``<EndNote><Cite>…</Cite></EndNote>`` instruction payload for
    one in-text citation of ``source``."""
    return "<EndNote>" + _cite_element(source, notes, cite_number) + "</EndNote>"


def citation_group_payload(
    cites: list[tuple[dict[str, Any], str | None, int]],
) -> str:
    """The ``<EndNote>…</EndNote>`` instruction payload for a citation run —
    one ``<Cite>`` element per ``(source, notes, cite_number)`` triple in a
    single wrapper, exactly how EndNote itself writes a grouped citation
    (``ADDIN EN.CITE <EndNote><Cite>…</Cite><Cite>…</Cite></EndNote>``)."""
    return (
        "<EndNote>"
        + "".join(_cite_element(source, notes, n) for source, notes, n in cites)
        + "</EndNote>"
    )


# ── OOXML field emission (hand-rolled complex fields) ─────────────────


def _run(child: Any) -> Any:
    from docx.oxml import OxmlElement

    r = OxmlElement("w:r")
    r.append(child)
    return r


def _fld_char(kind: str) -> Any:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    fc = OxmlElement("w:fldChar")
    fc.set(qn("w:fldCharType"), kind)
    return _run(fc)


def _instr_text(text: str) -> Any:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    el = OxmlElement("w:instrText")
    el.set(qn("xml:space"), "preserve")
    el.text = text
    return _run(el)


def _cached_run(text: str) -> Any:
    """A ``<w:noProof/>`` run holding cached field-result text (EndNote marks
    its generated text no-proof so the spell-checker skips it)."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    r = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    rpr.append(OxmlElement("w:noProof"))
    r.append(rpr)
    t = OxmlElement("w:t")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    r.append(t)
    return r


def add_citation_field(
    paragraph: Any, cites: list[tuple[dict[str, Any], str | None, int]]
) -> None:
    """Append ONE ``ADDIN EN.CITE`` complex field to ``paragraph`` — the
    in-text citation. ``cites`` carries one ``(source, notes, cite_number)``
    triple per cited reference: a run of adjacent citations emits a single
    field with several ``<Cite>`` elements, exactly as EndNote itself writes
    a grouped citation (one cite → one ``<Cite>``). Shape mirrors a real
    EndNote field: begin · instrText · separate · cached display · end — the
    cached display is the merged numeric mark (``[1, 2]`` / ``[1–3]``)
    matching the numbered bibliography. Each ``notes`` embeds the cited
    passage into its record's Research Notes."""
    p = paragraph._p
    payload = citation_group_payload(cites)
    p.append(_fld_char("begin"))
    p.append(_instr_text(" ADDIN EN.CITE " + payload))
    p.append(_fld_char("separate"))
    p.append(_cached_run(merged_cite_numbers(sorted(n for _s, _n, n in cites))))
    p.append(_fld_char("end"))


def add_reflist_field(doc: Any, cached_lines: list[str]) -> None:
    """Append the ``ADDIN EN.REFLIST`` bibliography field. EndNote
    regenerates the list on update, so ``cached_lines`` is just placeholder
    text shown until then (one paragraph per reference, matching how EndNote
    lays the formatted list out)."""
    from docx.oxml import OxmlElement

    para = doc.add_paragraph()
    p = para._p
    p.append(_fld_char("begin"))
    p.append(_instr_text(" ADDIN EN.REFLIST "))
    p.append(_fld_char("separate"))
    for i, line in enumerate(cached_lines):
        if i:
            br = OxmlElement("w:r")
            br.append(OxmlElement("w:br"))
            p.append(br)
        p.append(_cached_run(line))
    p.append(_fld_char("end"))


# ── document-level EndNote state (docVars EndNote reads) ──────────────

_INSTANT_FORMAT = (
    "<ENInstantFormat><Enabled>1</Enabled><ScanUnformatted>1</ScanUnformatted>"
    "<ScanChanges>1</ScanChanges><Suspended>0</Suspended></ENInstantFormat>"
)


def _layout(style: str) -> str:
    return (
        f"<ENLayout><Style>{esc(style)}</Style><LeftDelim>{{</LeftDelim>"
        "<RightDelim>}</RightDelim><FontName>Calibri</FontName><FontSize>11"
        "</FontSize><ReflistTitle></ReflistTitle><StartingRefnum>1"
        "</StartingRefnum><FirstLineIndent>0</FirstLineIndent><HangingIndent>720"
        "</HangingIndent><LineSpacing>0</LineSpacing><SpaceAfter>0</SpaceAfter>"
        "<HyperlinksEnabled>0</HyperlinksEnabled><HyperlinksVisible>0"
        "</HyperlinksVisible><EnableBibliographyCategories>0"
        "</EnableBibliographyCategories></ENLayout>"
    )


def install_document_vars(doc: Any, rec_numbers: list[int], *, style: str) -> None:
    """Add the ``EN.InstantFormat`` / ``EN.Layout`` / ``EN.Libraries``
    document variables to ``word/settings.xml`` so EndNote's CWYW knows the
    doc carries its citations (and, with instant-format on, scans + formats
    them on open). Idempotent-ish: appends a fresh ``<w:docVars>`` block."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    settings = doc.settings.element
    docvars = OxmlElement("w:docVars")

    def _var(name: str, val: str) -> None:
        dv = OxmlElement("w:docVar")
        dv.set(qn("w:name"), name)
        dv.set(qn("w:val"), val)  # lxml escapes the attribute value
        docvars.append(dv)

    ids = "".join(f"<item>{n}</item>" for n in rec_numbers)
    libraries = (
        f'<Libraries><item db-id="{_DB_ID}">precis traveling library'
        f"<record-ids>{ids}</record-ids></item></Libraries>"
    )
    _var("EN.InstantFormat", _INSTANT_FORMAT)
    _var("EN.Layout", _layout(style))
    _var("EN.Libraries", libraries)
    # docVars must sit at the top of <w:settings> per the schema ordering.
    settings.insert(0, docvars)

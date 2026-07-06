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

from typing import Any
from xml.sax.saxutils import escape as _xml_escape

from precis.utils.authors import author_display

#: EndNote ``ref-type`` codes (name, numeric code), verified against a real
#: EndNote-authored field (Journal Article = 17). The others are the standard
#: EndNote type codes; a cited precis ref maps by kind, defaulting to journal.
_REF_TYPES: dict[str, tuple[str, int]] = {
    "paper": ("Journal Article", 17),
    "patent": ("Patent", 25),
    "book": ("Book", 6),
    "report": ("Report", 10),
}

#: A single synthetic "traveling library" id shared by every emitted record.
#: EndNote keys a source on ``(db-id, rec-number)`` but formats from the
#: embedded ``<record>`` when the id isn't a live local library — which is
#: always our case, so a stable placeholder is correct and keeps exports
#: deterministic.
_DB_ID = "precismcp0traveling0library000000000"
_TIMESTAMP = "1700000000"  # fixed → deterministic export bytes


def _esc(value: Any) -> str:
    """XML-escape a text value for embedding in the EndNote payload."""
    return _xml_escape("" if value is None else str(value))


def _author_list(authors: list[dict[str, Any]] | None) -> list[str]:
    """Each author as EndNote's ``<author>`` string. Prefer ``Family, Given``
    (EndNote's canonical parse order); fall back to the display name."""
    out: list[str] = []
    for a in authors or []:
        name = author_display(a, order="sortable")  # "Family, Given"
        if name:
            out.append(name)
    return out


def _first_family(authors: list[dict[str, Any]] | None) -> str:
    """The lead author's family name (best effort) for the cached in-text
    display stub — ``author_display`` sortable form is ``Family, Given`` so
    the family is the head; a bare ``{"name"}`` falls back to the last token."""
    for a in authors or []:
        if isinstance(a, dict) and (a.get("family") or "").strip():
            return str(a["family"]).strip()
        disp = author_display(a, order="sortable")
        if disp:
            return disp.split(",")[0].strip() or disp.split()[-1]
    return ""


def _display_text(authors: list[dict[str, Any]] | None, year: int | None) -> str:
    """Cached in-text mark shown until EndNote reformats — an author-year
    stub like ``(Nasibulin 2007)`` (EndNote overwrites it on update)."""
    first = _first_family(authors)
    yr = str(year) if year else ""
    if first and len(authors or []) > 1:
        first += " et al."
    inside = " ".join(x for x in (first, yr) if x) or "citation"
    return f"({inside})"


#: cap on an embedded cited-passage note so one field's XML stays sane.
_NOTE_MAX_CHARS = 4000


def build_record(source: dict[str, Any], notes: str | None = None) -> str:
    """One EndNote ``<record>…</record>`` from a resolved source dict
    (``kind``, ``tag``, ``rec_number``, ``authors``, ``title``, ``year``,
    ``journal``, ``volume``, ``doi``, ``url``). Absent fields are omitted.

    ``notes`` (the cited passage from a ``pc<id>`` chunk citation) is embedded
    as ``<research-notes>`` — EndNote's *Research Notes* field — so the author
    sees the exact cited text on the citation. (Caveat: EndNote drops
    Research Notes when a traveling library is imported into a real library;
    the passage is present in the field data regardless.)"""
    name, code = _REF_TYPES.get(source.get("kind", "paper"), _REF_TYPES["paper"])
    rec = source["rec_number"]
    parts = [
        "<record>",
        f"<rec-number>{rec}</rec-number>",
        f'<foreign-keys><key app="EN" db-id="{_DB_ID}" '
        f'timestamp="{_TIMESTAMP}">{rec}</key></foreign-keys>',
        f'<ref-type name="{_esc(name)}">{code}</ref-type>',
    ]
    authors = _author_list(source.get("authors"))
    if authors:
        inner = "".join(f"<author>{_esc(a)}</author>" for a in authors)
        parts.append(f"<contributors><authors>{inner}</authors></contributors>")
    titles = [f"<title>{_esc(source.get('title'))}</title>"]
    if source.get("journal"):
        titles.append(f"<secondary-title>{_esc(source['journal'])}</secondary-title>")
    parts.append("<titles>" + "".join(titles) + "</titles>")
    if source.get("journal"):
        parts.append(
            f"<periodical><full-title>{_esc(source['journal'])}"
            "</full-title></periodical>"
        )
    if source.get("volume"):
        parts.append(f"<volume>{_esc(source['volume'])}</volume>")
    if source.get("year"):
        parts.append(f"<dates><year>{_esc(source['year'])}</year></dates>")
    if source.get("doi"):
        # EndNote stores the DOI in <electronic-resource-num>.
        parts.append(
            f"<electronic-resource-num>{_esc(source['doi'])}</electronic-resource-num>"
        )
    if source.get("url"):
        parts.append(
            f"<urls><related-urls><url>{_esc(source['url'])}</url>"
            "</related-urls></urls>"
        )
    else:
        parts.append("<urls></urls>")
    if notes:
        text = notes.strip()
        if len(text) > _NOTE_MAX_CHARS:
            text = text[:_NOTE_MAX_CHARS].rstrip() + "…"
        parts.append(f"<research-notes>{_esc(text)}</research-notes>")
    parts.append("</record>")
    return "".join(parts)


def citation_payload(source: dict[str, Any], notes: str | None = None) -> str:
    """The full ``<EndNote><Cite>…</Cite></EndNote>`` instruction payload for
    one in-text citation of ``source``. ``notes`` is the cited passage
    (``pc<id>`` chunk citation) → the record's ``<research-notes>``."""
    first_family = _first_family(source.get("authors"))
    year = source.get("year")
    display = _display_text(source.get("authors"), year)
    rec = source["rec_number"]
    return (
        "<EndNote><Cite>"
        f"<Author>{_esc(first_family)}</Author>"
        f"<Year>{_esc(year) if year else ''}</Year>"
        f"<RecNum>{rec}</RecNum>"
        f"<DisplayText>{_esc(display)}</DisplayText>"
        f"{build_record(source, notes)}"
        "</Cite></EndNote>"
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
    paragraph: Any, source: dict[str, Any], notes: str | None = None
) -> None:
    """Append an ``ADDIN EN.CITE`` complex field to ``paragraph`` — the
    in-text citation. Shape mirrors a real EndNote field: begin · instrText ·
    separate · cached ``(Author Year)`` display · end. ``notes`` embeds the
    cited passage into the record's Research Notes."""
    p = paragraph._p
    payload = citation_payload(source, notes)
    p.append(_fld_char("begin"))
    p.append(_instr_text(" ADDIN EN.CITE " + payload))
    p.append(_fld_char("separate"))
    p.append(_cached_run(_display_text(source.get("authors"), source.get("year"))))
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
        f"<ENLayout><Style>{_esc(style)}</Style><LeftDelim>{{</LeftDelim>"
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

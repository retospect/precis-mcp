"""Native Word footnotes for external supplement references."""

from __future__ import annotations

from typing import Any, cast


def add_footnote(paragraph: Any, text: str) -> None:
    """Append a footnote reference and its corresponding document part entry."""
    from docx.opc.constants import CONTENT_TYPE as CT
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.packuri import PackURI
    from docx.opc.part import XmlPart
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.oxml.xmlchemy import BaseOxmlElement

    part = next(
        (
            rel.target_part
            for rel in paragraph.part.rels.values()
            if rel.reltype == RT.FOOTNOTES
        ),
        None,
    )
    if part is None:
        root = OxmlElement("w:footnotes")
        for ident, kind in ((-1, "separator"), (0, "continuationSeparator")):
            note = OxmlElement("w:footnote")
            note.set(qn("w:id"), str(ident))
            note.set(qn("w:type"), kind)
            p = OxmlElement("w:p")
            r = OxmlElement("w:r")
            r.append(OxmlElement("w:" + kind))
            p.append(r)
            note.append(p)
            root.append(note)
        part = XmlPart(
            PackURI("/word/footnotes.xml"),
            CT.WML_FOOTNOTES,
            cast(BaseOxmlElement, root),
            paragraph.part.package,
        )
        paragraph.part.relate_to(part, RT.FOOTNOTES)
    ident = max(int(n.get(qn("w:id"))) for n in part.element) + 1
    marker = OxmlElement("w:footnoteReference")
    marker.set(qn("w:id"), str(ident))
    run = paragraph.add_run()
    run.font.superscript = True
    run._r.append(marker)
    note = OxmlElement("w:footnote")
    note.set(qn("w:id"), str(ident))
    p = OxmlElement("w:p")
    r = OxmlElement("w:r")
    r.append(OxmlElement("w:footnoteRef"))
    p.append(r)
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.set(qn("xml:space"), "preserve")
    t.text = " " + text
    r.append(t)
    p.append(r)
    note.append(p)
    part.element.append(note)

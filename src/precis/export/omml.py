"""LaTeX math → OMML (Office MathML) for the ``.docx`` export.

Word renders math as **OMML** (``<m:oMath>`` in WordprocessingML), not
MathML — MathML in a docx body simply doesn't render. So we go
``LaTeX → MathML`` (``latex2mathml``) → ``OMML`` with a **self-contained
recursive converter** for the core MathML element set. The canonical
MathML→OMML path is Microsoft's ``MML2OMML.xsl``, but that file isn't
redistributable with precis, so we transform the common constructs
directly here. Anything unsupported degrades to its text content rather
than failing the export.

Returns an lxml ``<m:oMath>`` element ready to append into a paragraph's
XML; ``None`` when conversion fails (the caller falls back to literal
text). The ``m`` namespace is declared on the returned root so it
serialises with the standard prefix.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"


def latex_to_omml(latex: str) -> Any | None:
    """``LaTeX`` math (no ``$``) → an ``<m:oMath>`` lxml element, or
    ``None`` if conversion isn't possible (missing dep / parse error /
    empty)."""
    latex = (latex or "").strip()
    if not latex:
        return None
    try:
        from latex2mathml.converter import convert
        from lxml import etree
    except Exception:  # pragma: no cover — optional deps absent
        return None
    try:
        mathml = convert(latex)
        root = etree.fromstring(mathml.encode("utf-8"))
    except Exception:
        log.debug("omml: latex2mathml failed for %r", latex, exc_info=True)
        return None
    omath = etree.Element(f"{{{_M}}}oMath", nsmap={"m": _M})
    try:
        _convert(root, omath, etree)
    except Exception:
        log.debug("omml: MathML→OMML failed for %r", latex, exc_info=True)
        return None
    return omath


def _localname(node: Any) -> str:
    from lxml import etree

    return str(etree.QName(node).localname)


def _run(parent: Any, text: str, etree: Any) -> None:
    """An OMML run: ``<m:r><m:t>text</m:t></m:r>``."""
    if not text:
        return
    r = etree.SubElement(parent, f"{{{_M}}}r")
    t = etree.SubElement(r, f"{{{_M}}}t")
    # Preserve surrounding spaces in operators like " = ".
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    t.text = text


def _box(parent: Any, tag: str, child: Any, etree: Any) -> Any:
    """An OMML container (``m:e`` / ``m:sup`` / ``m:num`` …) holding the
    converted ``child`` MathML node (or empty when ``child`` is None)."""
    box = etree.SubElement(parent, f"{{{_M}}}{tag}")
    if child is not None:
        _convert(child, box, etree)
    return box


def _convert(node: Any, parent: Any, etree: Any) -> None:
    """Recursively convert one MathML node, appending OMML to ``parent``."""
    name = _localname(node)
    if name in ("math", "mrow", "mstyle", "semantics"):
        for child in node:
            _convert(child, parent, etree)
        return
    if name in ("mi", "mn", "mo", "mtext", "ms"):
        _run(parent, node.text or "", etree)
        return
    kids = list(node)
    if name == "msup":
        s = etree.SubElement(parent, f"{{{_M}}}sSup")
        _box(s, "e", kids[0] if kids else None, etree)
        _box(s, "sup", kids[1] if len(kids) > 1 else None, etree)
        return
    if name == "msub":
        s = etree.SubElement(parent, f"{{{_M}}}sSub")
        _box(s, "e", kids[0] if kids else None, etree)
        _box(s, "sub", kids[1] if len(kids) > 1 else None, etree)
        return
    if name == "msubsup":
        s = etree.SubElement(parent, f"{{{_M}}}sSubSup")
        _box(s, "e", kids[0] if kids else None, etree)
        _box(s, "sub", kids[1] if len(kids) > 1 else None, etree)
        _box(s, "sup", kids[2] if len(kids) > 2 else None, etree)
        return
    if name == "mfrac":
        f = etree.SubElement(parent, f"{{{_M}}}f")
        _box(f, "num", kids[0] if kids else None, etree)
        _box(f, "den", kids[1] if len(kids) > 1 else None, etree)
        return
    if name in ("msqrt", "mroot"):
        rad = etree.SubElement(parent, f"{{{_M}}}rad")
        radpr = etree.SubElement(rad, f"{{{_M}}}radPr")
        if name == "msqrt":
            dh = etree.SubElement(radpr, f"{{{_M}}}degHide")
            dh.set(f"{{{_M}}}val", "1")
            _box(rad, "deg", None, etree)
            # msqrt's children are the radicand (often wrapped in mrow).
            e = etree.SubElement(rad, f"{{{_M}}}e")
            for c in kids:
                _convert(c, e, etree)
        else:  # mroot: [base, index]
            _box(rad, "deg", kids[1] if len(kids) > 1 else None, etree)
            _box(rad, "e", kids[0] if kids else None, etree)
        return
    if name in ("mfenced", "mrow"):  # delimiters → m:d
        d = etree.SubElement(parent, f"{{{_M}}}d")
        e = etree.SubElement(d, f"{{{_M}}}e")
        for c in kids:
            _convert(c, e, etree)
        return
    # Fallback: flatten any remaining text content into a run.
    text = "".join(node.itertext())
    _run(parent, text, etree)


__all__ = ["latex_to_omml"]

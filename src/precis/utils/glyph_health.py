"""Read side of the ingest glyph-health record (gr228652), layer-free.

``precis.ingest.glyph_health`` writes a per-document ``glyph_health``
record onto a paper's ``meta`` when a PDF font's encoding lies (the μ/m
substitutions that turn "6 µm" into "6 m", gr228594). These two helpers
read that record: the predicate :func:`glyph_suspected` and the one
caveat sentence :func:`glyph_caveat`. They live here — not in ingest —
because every surface needs them: the paper and finding views, the draft
write-path ``glyph_cite_hint``, and the measures write path, which sits
below ingest and may not import it. ``precis.ingest.glyph_health``
re-exports both for its ingest-side callers.
"""

from __future__ import annotations

from typing import Any


def glyph_suspected(meta: Any) -> bool:
    """True when ``meta`` (a ref's meta dict) carries a suspected
    ``glyph_health`` record. Tolerates ``None`` / non-dict / malformed."""
    if not isinstance(meta, dict):
        return False
    rec = meta.get("glyph_health")
    return isinstance(rec, dict) and rec.get("suspected") is True


def glyph_caveat(meta: Any) -> str:
    """One-line caveat for a glyph-suspected paper (``""`` when clean).

    Names the suspect fonts / modes only when the record carries them.
    """
    if not glyph_suspected(meta):
        return ""
    rec = meta["glyph_health"]
    detail: list[str] = []
    fonts = [str(f) for f in (rec.get("suspect_fonts") or [])]
    if fonts:
        detail.append("fonts: " + ", ".join(fonts))
    modes = [str(m) for m in (rec.get("modes") or [])]
    if modes:
        detail.append("modes: " + ", ".join(modes))
    suffix = f" ({'; '.join(detail)})" if detail else ""
    return (
        "Text extraction may have dropped or substituted Greek/μ "
        f"characters in this paper{suffix}. Check numbers and units against "
        "the PDF before quoting."
    )

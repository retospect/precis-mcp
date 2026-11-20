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

Paper chunk reads also check stored passages for recognizable glyph
substitutions so older extractions can warn without ingest-time font evidence.
"""

from __future__ import annotations

import re
from typing import Any

# Require surrounding syntax that makes the substitution suspicious. Bare
# numbers, milli-units, and plain-text exponents are common in intact papers.
_PASSAGE_DAMAGE_RE = re.compile(
    r"(?<![\w.,])\d[\d,.]*[ \t]+8[CF]\b"
    r"|\b[A-Za-z][ \t]+¼[ \t]+[+-]?\d+:\d+\b"
    r"|\b(?:contact |cone |tilt )?angles? of [ \t]*\d+(?:\.\d+)?u\b"
    r"|\b\d+8[ \t]+kQ\b"
    r"|\bV[ \t]*/[ \t]+mm\b"
    r"|\bV[ \t]+mm2<sup>1</sup>"
    r"|Â±|Ã\x82±"
)


def passage_glyph_caveat(text: str) -> str:
    """Warn about positive corruption signatures in a stored passage."""
    if not _PASSAGE_DAMAGE_RE.search(text):
        return ""
    return (
        "Text extraction may have substituted scientific symbols in this passage. "
        "Check numbers and units against the PDF before quoting."
    )


def glyph_suspected(meta: Any) -> bool:
    """True when ``meta`` (a ref's meta dict) carries a suspected
    ``glyph_health`` record. Tolerates ``None`` / non-dict / malformed."""
    if not isinstance(meta, dict):
        return False
    rec = meta.get("glyph_health")
    return isinstance(rec, dict) and rec.get("suspected") is True


def glyph_caveat(meta: Any) -> str:
    """One-line caveat for a glyph-suspected paper (``""`` when clean).

    Names the suspect fonts / modes / sign damage only when the record
    carries them. A sign-damage-only record (gr685717 — lost minus signs,
    mangled ±) gets its own lead sentence; a record carrying font evidence
    keeps the Greek/μ wording with the sign damage noted in the detail.
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
    sign_rec = rec.get("sign_damage")
    sign_damage = isinstance(sign_rec, dict) and sign_rec.get("detected") is True
    if sign_damage:
        detail.append("sign damage: minus/±")
    suffix = f" ({'; '.join(detail)})" if detail else ""
    if fonts or modes:
        lead = (
            "Text extraction may have dropped or substituted Greek/μ "
            "characters in this paper"
        )
    elif sign_damage:
        lead = (
            "Text extraction may have dropped minus signs or ± characters in this paper"
        )
    else:
        lead = (
            "Text extraction may have dropped or substituted characters in this paper"
        )
    return f"{lead}{suffix}. Check numbers and units against the PDF before quoting."

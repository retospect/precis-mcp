"""Read side of the ingest glyph-health record (gr228652), layer-free.

``precis.ingest.glyph_health`` writes a per-document ``glyph_health``
record onto a paper's ``meta`` when a PDF font's encoding lies (the μ/m
substitutions that turn "6 µm" into "6 m", gr228594). These helpers read
that record: the predicate :func:`glyph_suspected` and the one
caveat sentence :func:`glyph_caveat`. They live here — not in ingest —
because every surface needs them: the paper and finding views, the draft
write-path ``glyph_cite_hint``, and the measures write path, which sits
below ingest and may not import it. ``precis.ingest.glyph_health``
re-exports both for its ingest-side callers.

Paper chunk reads also check stored passages for recognizable glyph
substitutions so older extractions can warn without ingest-time font
evidence.

Correction records (gr724751, ``chunk_corrections``) ride the same
helpers: :func:`span_in_text` is the whitespace-tolerant span match that
anchors a correction to a passage across re-extraction, and
:func:`passage_glyph_caveat` takes the spans already covered by a
correction so a resolved passage stops warning.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
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

#: Whitespace runs, collapsed on both sides of a span match — extraction
#: re-flows text, so a verbatim quoted span must not fail on line breaks.
_WS_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def span_in_text(span: str, text: str) -> bool:
    """Whitespace-tolerant substring test — the correction anchor.

    Both sides are whitespace-collapsed before matching, so a span quoted
    from a re-flowed extraction still matches the stored passage (and a
    re-extracted passage still matches the recorded span, gr724751's
    paper-plus-quoted-span anchor).
    """
    s = _norm(span)
    return bool(s) and s in _norm(text)


def passage_glyph_caveat(text: str, corrected_spans: Iterable[str] = ()) -> str:
    """Warn about positive corruption signatures in a stored passage.

    ``corrected_spans`` — extracted spans already covered by a correction
    record (gr724751) — shrink the warning: when every damage signature
    lies inside a covered span the passage reads as resolved and the
    caveat is empty. The stored body is never rewritten; the correction
    renders alongside it.
    """
    matches = list(_PASSAGE_DAMAGE_RE.finditer(text))
    if not matches:
        return ""
    spans = [s for s in (_norm(s) for s in corrected_spans) if s]
    if spans and _all_covered(matches, spans, _norm(text)):
        return ""
    return (
        "Text extraction may have substituted scientific symbols in this passage. "
        "Check numbers and units against the PDF before quoting."
    )


def _all_covered(
    matches: list[re.Match[str]], spans: list[str], norm_text: str
) -> bool:
    """True when every damage match lies inside an occurrence of a
    corrected span (positions computed on the whitespace-collapsed text).
    """
    occs = {s: _find_all(norm_text, s) for s in spans}
    for m in matches:
        m_norm = _norm(m.group(0))
        starts = _find_all(norm_text, m_norm)
        if not starts:
            return False
        # Every occurrence of the damage token must be covered, not just
        # the first: a repeated token with one covered copy stays flagged.
        for start in starts:
            end = start + len(m_norm)
            if not any(
                occ <= start and occ + len(s) >= end
                for s, found in occs.items()
                for occ in found
            ):
                return False
    return True


def _find_all(text: str, needle: str) -> list[int]:
    out: list[int] = []
    pos = text.find(needle)
    while pos >= 0:
        out.append(pos)
        pos = text.find(needle, pos + 1)
    return out


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

"""Narration layer — turn any draft into a *voice score*.

Cross-cutting, NOT a doc type: every draft (patent / techreport / paper / …)
narrates through this one layer, just as they all export to docx/pdf. The
layer reads three things off a draft and never writes back:

- **who says it** — per-chunk ``meta.voice`` / ``meta.lang`` (falling back to a
  draft- or call-level default). Chunks already carry a ``meta`` dict, so this
  is metadata, no new kind and no migration.
- **the words** — the chunk prose, made *speakable*: inline handles
  (``[pc12]`` / ``[[dc4]]``), math, code fences and markdown emphasis are
  stripped/spoken so the ear gets clean text, never raw markup.
- **how special words sound** — an optional pronunciation **lexicon**
  (``surface → respelling``), the abbrev-class feature: keep the prose clean
  and fix "precis", arXiv, names, jargon out-of-band.

Output is a list of :class:`NarrationSegment` (text + voice + lang + kind); the
audio renderer (:mod:`precis.export.audio`) drives a TTS synth from it. This
module is pure (store reads only) and TTS-agnostic, so it unit-tests without a
model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Chunk kinds skipped for the ear (v1): structural containers, and
# figures/tables/code/glossary that don't read aloud as prose.
_SKIP_KINDS = frozenset({"ulist", "olist", "figure", "table", "code", "term"})

# Inline draft handles / citations: [pc12], [[dc4]], [§a~3]. Dropped for the ear.
_REF = re.compile(r"\[\[[^\]]+\]\]|\[(?:[a-z]{2}\d+[a-z0-9~]*|§[^\]]+)\]")
_DISPLAY_MATH = re.compile(r"\$\$.+?\$\$", re.DOTALL)
_INLINE_MATH = re.compile(r"\$[^$]+\$")
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC = re.compile(r"(?<![*\w])\*(?!\s)([^*]+?)(?<!\s)\*(?!\w)")
_SUBSUP = re.compile(r"</?su[bp]>")
# Stray LaTeX artifacts that leak into prose: ``\\`` line-breaks and bare
# ``^`` / ``_`` sup/sub carets (``sp^2`` → ``sp2``), which otherwise read as
# "backslash backslash" / "caret". Math proper is already dropped above.
_LATEX_BREAK = re.compile(r"\\\\")
_CARET = re.compile(r"[\^](?=\w)")
_WS = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class NarrationSegment:
    """One speakable span: the text, who narrates it, and its source kind
    (which drives the leading pause — headings breathe longer)."""

    text: str
    voice: str
    lang: str
    kind: str


def speakable(text: str) -> str:
    """Strip draft markup so the ear gets clean prose.

    Handles/citations vanish; display math becomes a spoken "equation" cue;
    inline math + emphasis markers are unwrapped; whitespace collapses.
    """
    t = _REF.sub("", text)
    t = _DISPLAY_MATH.sub(" equation, ", t)
    t = _INLINE_MATH.sub("", t)
    t = _CODE.sub(r"\1", t)
    t = _BOLD.sub(r"\1", t)
    t = _ITALIC.sub(r"\1", t)
    t = _SUBSUP.sub("", t)
    t = _LATEX_BREAK.sub(" ", t)
    t = _CARET.sub("", t)
    return _WS.sub(" ", t).strip()


def apply_lexicon(text: str, lexicon: dict[str, str] | None) -> str:
    """Substitute pronunciation respellings for surface forms (whole-word,
    case-insensitive, longest-first so 'arXiv' wins over 'ar'). The abbrev-
    class pronunciation layer — out-of-band, prose stays clean."""
    if not lexicon:
        return text
    for surface in sorted(lexicon, key=len, reverse=True):
        text = re.compile(rf"\b{re.escape(surface)}\b", re.IGNORECASE).sub(
            lexicon[surface].replace("\\", r"\\"), text
        )
    return text


def render_narration(
    store: Any,
    ref: Any,
    *,
    default_voice: str,
    default_lang: str,
    lexicon: dict[str, str] | None = None,
) -> list[NarrationSegment]:
    """Walk a draft in reading order → the voice score.

    Per-chunk ``meta.voice`` / ``meta.lang`` win over the defaults; empty /
    skipped chunks drop out. A draft-level default can be threaded via the
    ``default_*`` args (e.g. from doc-class defaults or the draft's own meta).
    """
    segments: list[NarrationSegment] = []
    for c in store.reading_order(ref.id):
        if c.chunk_kind in _SKIP_KINDS:
            continue
        spoken = apply_lexicon(speakable(c.text or ""), lexicon)
        if not spoken:
            continue
        meta = c.meta or {}
        segments.append(
            NarrationSegment(
                text=spoken,
                voice=str(meta.get("voice") or default_voice),
                lang=str(meta.get("lang") or default_lang),
                kind=c.chunk_kind,
            )
        )
    return segments

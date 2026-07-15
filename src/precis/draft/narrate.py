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
# Stray backslashes that leak into prose — a LaTeX line-break ``\\``, a
# control-space ``\ ``, an escaped char (``\%`` / ``\_``), or a leaked macro
# (``\gamma``) — are voiced by the TTS as the *word* "backslash". Drop any run
# of them (→ a space, so line-breaks still separate words). Runs after math, so
# real equations aren't touched. Plus bare ``^`` sup/sub carets (``sp^2`` →
# ``sp2``), which otherwise read as "caret".
_BACKSLASH = re.compile(r"\\+")
_CARET = re.compile(r"[\^](?=\w)")
_WS = re.compile(r"\s+")

# Markdown prose (the news-briefing producer path, not draft chunks): a link
# ``[text](url)`` reads as its anchor text; bare URLs and heading/list markers
# are dropped for the ear.
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]*)\)")
_BARE_URL = re.compile(r"https?://\S+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.MULTILINE)


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
    t = _BACKSLASH.sub(" ", t)
    t = _CARET.sub("", t)
    return _WS.sub(" ", t).strip()


def speakable_markdown(text: str) -> str:
    """Clean a *markdown* block for the ear: resolve links to their anchor text,
    drop bare URLs, strip heading/list markers, then run the draft
    :func:`speakable` cleanup (emphasis / code / handles / math). The prose path
    for non-draft producers — the news briefing is markdown, not draft chunks."""
    t = _MD_LINK.sub(r"\1", text)
    t = _BARE_URL.sub("", t)
    t = _HEADING.sub("", t)
    t = _BULLET.sub("", t)
    return speakable(t)


def markdown_segments(
    text: str,
    *,
    voice: str,
    lang: str,
    lexicon: dict[str, str] | None = None,
) -> list[NarrationSegment]:
    """Split markdown prose into speakable narration segments — one per block.

    Blocks split on blank lines; a block whose first line is a heading (``#``)
    becomes a ``heading`` segment (longer leading pause), the rest ``para``.
    Markup is stripped via :func:`speakable_markdown` and the lexicon applied;
    empty blocks drop out. Single-voice (no per-chunk meta, unlike a draft) —
    the news-briefing producer renders through this."""
    segments: list[NarrationSegment] = []
    for raw in re.split(r"\n\s*\n", text.strip()):
        block = raw.strip()
        if not block:
            continue
        kind = "heading" if block.lstrip().startswith("#") else "para"
        spoken = apply_lexicon(speakable_markdown(block), lexicon)
        if spoken:
            segments.append(
                NarrationSegment(text=spoken, voice=voice, lang=lang, kind=kind)
            )
    return segments


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


def load_personal_lexicon(path: str | None = None) -> dict[str, str]:
    """The cross-draft pronunciation base — ``{surface: respelling}`` JSON at
    ``PRECIS_LEXICON_FILE`` (or ``path``). Words you say the same in *every*
    draft (your name, "precis", "arXiv") live here so you teach them once.
    Empty + forgiving: unset / missing / malformed → ``{}``."""
    import json
    import os
    from pathlib import Path

    p = path or os.environ.get("PRECIS_LEXICON_FILE")
    if not p:
        return {}
    try:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def resolve_lexicon(
    ref: Any, *, personal: dict[str, str] | None = None
) -> dict[str, str]:
    """Merge the two-level pronunciation lexicon for a draft.

    Personal (cross-draft base) UNDER the draft's own overrides — ``ref.meta``
    ``pronunciation`` dict, per-document like abbrevs but a dedicated free
    lexicon (so it covers names/words that aren't glossary terms). Per-draft
    wins on a clash.
    """
    merged: dict[str, str] = dict(personal or {})
    draft_lex = (getattr(ref, "meta", None) or {}).get("pronunciation")
    if isinstance(draft_lex, dict):
        merged.update({str(k): str(v) for k, v in draft_lex.items()})
    return merged


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

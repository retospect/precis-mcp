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

# Where CJK spans route by default when a block mixes scripts (see
# ``split_by_script``). ``ja`` / ``jf_alpha`` is the live need (the Japanese
# reading cast); a Chinese draft overrides via ``meta.cjk_lang='cmn'``.
_DEFAULT_CJK_VOICE = "jf_alpha"
_DEFAULT_CJK_LANG = "ja"

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


#: Fullwidth ASCII (U+FF01–FF5E) → ASCII (U+21–7E) is a constant −0xFEE0 shift.
_FULLWIDTH_ASCII_OFFSET = 0xFEE0


def _is_cjk(ch: str) -> bool:
    """A char that a Latin/espeak voice cannot read: CJK symbols/punctuation,
    kana + the kana-adjacent scripts in the U+3000–33FF band (Bopomofo,
    katakana phonetic ext, CJK strokes, enclosed/compat CJK), CJK Han (main +
    ext-A + compat, and astral ext-B+), and the fullwidth/halfwidth forms —
    but **not** fullwidth ASCII (U+FF01–FF5E), which is Latin letters / digits /
    punctuation the base voice reads once folded (see :func:`_fold_fullwidth`)."""
    o = ord(ch)
    if 0xFF01 <= o <= 0xFF5E:
        return False  # fullwidth Latin/ASCII — belongs to the base voice
    return (
        0x3000 <= o <= 0x33FF
        or 0x3400 <= o <= 0x9FFF
        or 0xF900 <= o <= 0xFAFF
        or 0xFF00 <= o <= 0xFFEF
        or 0x20000 <= o <= 0x2FA1F
    )


def _has_kana(s: str) -> bool:
    """True if ``s`` contains hiragana/katakana (U+3040–30FF) or the katakana
    phonetic extensions (U+31F0–31FF, Ainu) — unambiguously Japanese, so the
    run routes to ``ja`` regardless of the CJK-lang default."""
    return any(0x3040 <= ord(c) <= 0x30FF or 0x31F0 <= ord(c) <= 0x31FF for c in s)


def _fold_fullwidth(s: str) -> str:
    """Fold fullwidth ASCII (U+FF01–FF5E) to plain ASCII so the base (Latin)
    voice reads ``ＡＢＣ１２３`` as ``ABC123`` instead of it being handed to a
    CJK voice and misvoiced."""
    return "".join(
        chr(ord(c) - _FULLWIDTH_ASCII_OFFSET) if 0xFF01 <= ord(c) <= 0xFF5E else c
        for c in s
    )


def _resolve_cjk(voice: Any, lang: Any) -> tuple[str, str]:
    """Reconcile the per-chunk CJK ``(voice, lang)`` routing pair so the two
    always agree — a voice must speak the language it is tagged with, else the
    text is phonemized in one language and spoken by another (garbage / a hard
    synth failure).

    ``meta.cjk_voice`` / ``meta.cjk_lang`` are independent knobs, so a chunk may
    set only one. Resolution (values coerced to ``str``; empty / ``None`` →
    "unset"):

    - neither → the default pair (``ja`` / ``jf_alpha``, the live Japanese cast);
    - only ``lang`` → that language's default voice (a Chinese draft that sets
      ``cjk_lang='cmn'`` alone now gets ``zf_xiaoxiao``, not the Japanese
      default — the bug this fixes);
    - only ``voice`` → the language that voice speaks;
    - both, but disagreeing → ``lang`` wins (the explicit routing intent) and its
      default voice replaces the mismatched one.

    An unknown voice / lang falls back to the default rather than raising — a
    typo must not fail a whole cast mid-render (the draft handler validates
    ``voice``/``lang`` on write; this is the render-time backstop).
    """
    from precis.tts import voices as _voices

    v = str(voice) if voice else None
    ln = str(lang) if lang else None
    if v is None and ln is None:
        return _DEFAULT_CJK_VOICE, _DEFAULT_CJK_LANG
    if v is None:
        assert ln is not None  # narrowed by the guard above
        return (_voices.default_voice_for_lang(ln) or _DEFAULT_CJK_VOICE), ln
    if ln is None:
        return v, (_voices.lang_for_voice(v) or _DEFAULT_CJK_LANG)
    if _voices.lang_for_voice(v) == ln:
        return v, ln
    return (_voices.default_voice_for_lang(ln) or _DEFAULT_CJK_VOICE), ln


def split_by_script(
    text: str,
    *,
    base_voice: str,
    base_lang: str,
    cjk_voice: str | None = None,
    cjk_lang: str | None = None,
    kana_voice: str | None = None,
) -> list[tuple[str, str, str]]:
    """Split ``text`` into ``(text, voice, lang)`` spans on CJK boundaries.

    The fix for the "unknown Japanese character" symptom: a Latin (espeak) voice
    has no reading for kana/kanji, so a Japanese run inside an English block must
    be handed to a Japanese voice/engine instead. A contiguous CJK run becomes a
    ``(cjk_voice, cjk_lang)`` span (kana ⇒ ``ja`` unambiguously; Han-only ⇒ the
    ``cjk_lang`` default); everything else keeps ``(base_voice, base_lang)``.
    Adjacent same-``(voice, lang)`` spans coalesce.

    Pure single-script text (the overwhelmingly common case — every English
    briefing / meditation) returns a single unchanged base span, so this is a
    no-op there and the existing segment output is byte-identical.

    ``cjk_voice`` / ``cjk_lang`` are reconciled through :func:`_resolve_cjk`, so
    a caller may pass only one (e.g. ``cjk_lang='cmn'``) and get a coherent
    voice↔lang pair rather than the Japanese default voice mislabelled Chinese.

    ``kana_voice`` overrides the Japanese voice used for a kana run inside a
    non-Japanese (e.g. Chinese-default) block; when unset it derives from the
    voices catalogue's ``ja`` default rather than a hardcoded id. A block that
    is itself Japanese (``cjk_lang == 'ja'``) uses ``cjk_voice`` throughout.
    """
    cjk_voice, cjk_lang = _resolve_cjk(cjk_voice, cjk_lang)
    if not any(_is_cjk(c) for c in text):
        return [(_fold_fullwidth(text), base_voice, base_lang)]

    runs: list[tuple[bool, list[str]]] = []
    for ch in text:
        flag = _is_cjk(ch)
        if runs and runs[-1][0] == flag:
            runs[-1][1].append(ch)
        else:
            runs.append((flag, [ch]))

    if cjk_lang == "ja":
        ja_voice = cjk_voice
    else:
        from precis.tts import voices as _voices

        ja_voice = (
            kana_voice or _voices.default_voice_for_lang("ja") or _DEFAULT_CJK_VOICE
        )
    spans: list[tuple[str, str, str]] = []
    for is_cjk, chars in runs:
        s = "".join(chars).strip()
        if not s:
            continue
        if not is_cjk:
            spans.append((_fold_fullwidth(s), base_voice, base_lang))
        elif _has_kana(s):
            spans.append((s, ja_voice, "ja"))
        else:
            spans.append((s, cjk_voice, cjk_lang))

    merged: list[tuple[str, str, str]] = []
    for s, v, ln in spans:
        if merged and merged[-1][1] == v and merged[-1][2] == ln:
            merged[-1] = (f"{merged[-1][0]} {s}", v, ln)
        else:
            merged.append((s, v, ln))
    return merged


def markdown_segments(
    text: str,
    *,
    voice: str,
    lang: str,
    lexicon: dict[str, str] | None = None,
    cjk_voice: str | None = None,
    cjk_lang: str | None = None,
    kana_voice: str | None = None,
) -> list[NarrationSegment]:
    """Split markdown prose into speakable narration segments — one per block.

    Blocks split on blank lines; a block whose first line is a heading (``#``)
    becomes a ``heading`` segment (longer leading pause), the rest ``para``.
    Markup is stripped via :func:`speakable_markdown` and the lexicon applied;
    empty blocks drop out. Single-voice base (no per-chunk meta, unlike a
    draft), but a block mixing scripts is split by :func:`split_by_script` so a
    Japanese span inside an English cast is voiced natively — the news-briefing
    producer and the Japanese reading cast both render through this."""
    segments: list[NarrationSegment] = []
    for raw in re.split(r"\n\s*\n", text.strip()):
        block = raw.strip()
        if not block:
            continue
        kind = "heading" if block.lstrip().startswith("#") else "para"
        spoken = apply_lexicon(speakable_markdown(block), lexicon)
        if not spoken:
            continue
        for seg_text, seg_voice, seg_lang in split_by_script(
            spoken,
            base_voice=voice,
            base_lang=lang,
            cjk_voice=cjk_voice,
            cjk_lang=cjk_lang,
            kana_voice=kana_voice,
        ):
            segments.append(
                NarrationSegment(
                    text=seg_text, voice=seg_voice, lang=seg_lang, kind=kind
                )
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
        for seg_text, seg_voice, seg_lang in split_by_script(
            spoken,
            base_voice=str(meta.get("voice") or default_voice),
            base_lang=str(meta.get("lang") or default_lang),
            cjk_voice=meta.get("cjk_voice"),
            cjk_lang=meta.get("cjk_lang"),
            kana_voice=meta.get("kana_voice"),
        ):
            segments.append(
                NarrationSegment(
                    text=seg_text,
                    voice=seg_voice,
                    lang=seg_lang,
                    kind=c.chunk_kind,
                )
            )
    return segments

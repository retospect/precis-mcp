"""The narration layer — draft → voice score (speakable text + per-chunk
voice/lang from meta + the pronunciation lexicon). Pure, no TTS."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from precis.draft.narrate import apply_lexicon, render_narration, speakable


@dataclass
class _Chunk:
    chunk_kind: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


class _Store:
    def __init__(self, chunks: list[_Chunk]) -> None:
        self._chunks = chunks

    def reading_order(self, _ref_id: int) -> list[_Chunk]:
        return self._chunks


class _Ref:
    id = 1


# ── speakable() ──────────────────────────────────────────────────────


def test_speakable_strips_handles_math_and_markdown():
    raw = "See [pc12] and [[dc4]]. The **rate** is $$k = A e^{-E/RT}$$ per `run`."
    out = speakable(raw)
    assert "[pc12]" not in out and "[[dc4]]" not in out
    assert "$$" not in out and "equation" in out
    assert "**" not in out and "rate" in out
    assert "`" not in out and "run" in out


def test_speakable_collapses_whitespace():
    assert speakable("a   b\n\nc") == "a b c"


def test_speakable_strips_latex_artifacts():
    # LaTeX line-break \\ and sup/sub carets leak into real drafts.
    assert speakable(r"Nanotubes\\ to sp^2 carbon") == "Nanotubes to sp2 carbon"


# ── lexicon ──────────────────────────────────────────────────────────


def test_lexicon_respells_whole_words_case_insensitive():
    lex = {"precis": "pray-see", "arXiv": "archive"}
    out = apply_lexicon("Precis indexes arXiv preprints.", lex)
    assert "pray-see" in out and "archive" in out
    # whole-word only — 'precise' must not be touched
    assert apply_lexicon("precisely", lex) == "precisely"


def test_lexicon_none_is_noop():
    assert apply_lexicon("hello", None) == "hello"


# ── render_narration() ───────────────────────────────────────────────


def test_per_chunk_voice_and_lang_win_over_defaults():
    store = _Store(
        [
            _Chunk("paragraph", "Default narrator here."),
            _Chunk("paragraph", "British bit.", {"voice": "bf_emma", "lang": "en-gb"}),
        ]
    )
    segs = render_narration(
        store, _Ref(), default_voice="af_heart", default_lang="en-us"
    )
    assert len(segs) == 2
    assert (segs[0].voice, segs[0].lang) == ("af_heart", "en-us")
    assert (segs[1].voice, segs[1].lang) == ("bf_emma", "en-gb")


def test_skips_structural_and_empty_chunks():
    store = _Store(
        [
            _Chunk("figure", "Figure 1 caption"),
            _Chunk("table", "| a | b |"),
            _Chunk("paragraph", "  [pc9]  "),  # nothing speakable after strip
            _Chunk("heading", "Introduction"),
            _Chunk("paragraph", "Real prose."),
        ]
    )
    segs = render_narration(
        store, _Ref(), default_voice="af_heart", default_lang="en-us"
    )
    kinds = [(s.kind, s.text) for s in segs]
    assert kinds == [("heading", "Introduction"), ("paragraph", "Real prose.")]


def test_lexicon_applied_during_render():
    store = _Store([_Chunk("paragraph", "precis rocks")])
    segs = render_narration(
        store,
        _Ref(),
        default_voice="af_heart",
        default_lang="en-us",
        lexicon={"precis": "pray-see"},
    )
    assert segs[0].text == "pray-see rocks"

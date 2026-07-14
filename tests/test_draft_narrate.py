"""The narration layer — draft → voice score (speakable text + per-chunk
voice/lang from meta + the pronunciation lexicon). Pure, no TTS."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from precis.draft.narrate import (
    apply_lexicon,
    load_personal_lexicon,
    markdown_segments,
    render_narration,
    resolve_lexicon,
    speakable,
    speakable_markdown,
)


class _RefMeta:
    def __init__(self, meta: dict[str, Any]) -> None:
        self.meta = meta


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


# ── two-level lexicon (personal base + per-draft override) ───────────


def test_resolve_lexicon_merges_draft_over_personal():
    ref = _RefMeta({"pronunciation": {"precis": "PRAY-see", "boxel": "box-ell"}})
    personal = {"precis": "pree-sis", "arXiv": "archive"}
    merged = resolve_lexicon(ref, personal=personal)
    assert merged["precis"] == "PRAY-see"  # draft wins over personal
    assert merged["arXiv"] == "archive"  # personal carries through
    assert merged["boxel"] == "box-ell"  # draft-only entry


def test_resolve_lexicon_handles_missing_and_bad_meta():
    assert resolve_lexicon(_RefMeta({}), personal={"a": "b"}) == {"a": "b"}
    assert resolve_lexicon(_RefMeta({"pronunciation": "oops"}), personal={}) == {}
    assert resolve_lexicon(_RefMeta({}), personal=None) == {}


def test_load_personal_lexicon(tmp_path, monkeypatch):
    monkeypatch.delenv("PRECIS_LEXICON_FILE", raising=False)
    assert load_personal_lexicon() == {}  # unset
    f = tmp_path / "lex.json"
    f.write_text('{"precis": "pray-see"}', encoding="utf-8")
    assert load_personal_lexicon(str(f)) == {"precis": "pray-see"}
    assert load_personal_lexicon(str(tmp_path / "nope.json")) == {}  # missing
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert load_personal_lexicon(str(bad)) == {}  # malformed


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


# --- markdown prose path (the news-briefing producer, not draft chunks) ---


def test_speakable_markdown_strips_links_urls_and_markers():
    # A markdown link reads as its anchor text; the URL is dropped.
    assert speakable_markdown("[Fed holds rates](https://x.com/a)") == "Fed holds rates"
    # Heading + list markers gone; bare URL dropped.
    assert speakable_markdown("## United States") == "United States"
    assert speakable_markdown("- a bullet point") == "a bullet point"
    # bare URL dropped, whitespace collapsed by speakable()
    assert speakable_markdown("see https://example.com/x now") == "see now"


def test_markdown_segments_splits_blocks_and_flags_headings():
    text = (
        "## Top stories\n\n"
        "[Rates held](https://x/a) steady today.\n\n"
        "- [Chip deal](https://x/b) closes.\n\n"
        "## United States\n\n"
        "**Worth watching**: the vote."
    )
    segs = markdown_segments(text, voice="af_heart", lang="en-us")
    kinds = [s.kind for s in segs]
    assert kinds == ["heading", "para", "para", "heading", "para"]
    assert segs[0].text == "Top stories"
    assert segs[1].text == "Rates held steady today."
    assert segs[2].text == "Chip deal closes."
    assert segs[4].text == "Worth watching: the vote."
    # Single voice throughout (no per-chunk meta on prose).
    assert all(s.voice == "af_heart" and s.lang == "en-us" for s in segs)


def test_markdown_segments_applies_lexicon_and_drops_empties():
    text = "## precis\n\n\n\nplain"
    segs = markdown_segments(
        text, voice="af_heart", lang="en-us", lexicon={"precis": "pray-see"}
    )
    assert [s.text for s in segs] == ["pray-see", "plain"]

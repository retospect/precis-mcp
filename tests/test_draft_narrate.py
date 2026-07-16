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
    split_by_script,
)
from precis.tts.voices import lang_for_voice


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


def test_speakable_strips_lone_backslashes():
    # A single stray backslash (control-space, escaped char, leaked macro) is
    # otherwise voiced as the word "backslash" (the boxel-review report).
    assert speakable(r"generated \ to") == "generated to"
    assert speakable(r"the \gamma factor") == "the gamma factor"
    assert speakable(r"about 50\% done") == "about 50 % done"
    assert "\\" not in speakable(r"a \ b \\ c \x")


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


# ── mixed-script narration (the "unknown Japanese character" fix) ─────


def test_split_by_script_noop_for_pure_latin():
    # Pure English is a single unchanged base span — byte-identical to today.
    out = split_by_script("the cat sleeps", base_voice="af_heart", base_lang="en-us")
    assert out == [("the cat sleeps", "af_heart", "en-us")]


def test_split_by_script_routes_kana_to_japanese():
    # Kana inside an English span → its own ja segment (jf_alpha); the English
    # keeps the base voice. This is the fix: espeak-en never sees the kana.
    out = split_by_script(
        "the word for cat is 猫 in Japanese",
        base_voice="af_heart",
        base_lang="en-us",
    )
    # Han-only 猫 with the default cjk_lang=ja → ja.
    assert ("猫", "jf_alpha", "ja") in out
    assert out[0] == ("the word for cat is", "af_heart", "en-us")
    assert all(lang in {"en-us", "ja"} for _t, _v, lang in out)


def test_split_by_script_kana_is_japanese_even_with_chinese_default():
    # A kana run is unambiguously Japanese regardless of the CJK-lang default.
    out = split_by_script(
        "say ねこ now",
        base_voice="af_heart",
        base_lang="en-us",
        cjk_voice="zf_xiaoxiao",
        cjk_lang="cmn",
    )
    assert ("ねこ", "jf_alpha", "ja") in out


def test_split_by_script_han_only_follows_cjk_default():
    # Han-only (no kana) routes to the configured default — Chinese here.
    out = split_by_script(
        "the character 猫 means cat",
        base_voice="af_heart",
        base_lang="en-us",
        cjk_voice="zf_xiaoxiao",
        cjk_lang="cmn",
    )
    assert ("猫", "zf_xiaoxiao", "cmn") in out


def test_split_by_script_lang_only_derives_matching_voice():
    # Regression: a chunk sets only cjk_lang='cmn' (no cjk_voice). The voice
    # must be derived from the lang (zf_xiaoxiao), NOT the Japanese default —
    # else Han spans get a Japanese voice tagged Chinese (garbage / synth fail).
    out = split_by_script(
        "the character 猫 means cat",
        base_voice="af_heart",
        base_lang="en-us",
        cjk_lang="cmn",
    )
    assert ("猫", "zf_xiaoxiao", "cmn") in out
    # Every CJK span's voice speaks its tagged language (the invariant).
    for _t, v, ln in out:
        if ln != "en-us":
            assert lang_for_voice(v) == ln


def test_split_by_script_voice_only_derives_matching_lang():
    # Symmetric: only cjk_voice set → lang derived from the voice, not the
    # Japanese default lang.
    out = split_by_script(
        "the character 猫 means cat",
        base_voice="af_heart",
        base_lang="en-us",
        cjk_voice="zf_xiaoxiao",
    )
    assert ("猫", "zf_xiaoxiao", "cmn") in out


def test_split_by_script_reconciles_mismatched_pair():
    # An incoherent explicit pair (a Japanese voice tagged Chinese) is
    # reconciled so voice and lang agree — lang wins, its default voice
    # replaces the mismatch, rather than emitting a garbage pair.
    out = split_by_script(
        "the character 猫 means cat",
        base_voice="af_heart",
        base_lang="en-us",
        cjk_voice="jf_alpha",
        cjk_lang="cmn",
    )
    han = [(t, v, ln) for t, v, ln in out if ln == "cmn"]
    assert han and all(lang_for_voice(v) == "cmn" for _t, v, _ln in han)


def test_split_by_script_kana_voice_overridable_in_chinese_block():
    # A kana run inside a Chinese-default block used to be forced to the
    # hardcoded jf_alpha; kana_voice now overrides it (gripe 161851 #1).
    out = split_by_script(
        "猫 は ねこ です",
        base_voice="af_heart",
        base_lang="en-us",
        cjk_voice="zf_xiaoxiao",
        cjk_lang="cmn",
        kana_voice="jf_nezumi",
    )
    assert any(v == "jf_nezumi" and ln == "ja" for _t, v, ln in out)
    # Han-only spans still follow the Chinese default.
    assert any(v == "zf_xiaoxiao" and ln == "cmn" for _t, v, ln in out)


def test_split_by_script_folds_fullwidth_ascii_to_base_voice():
    # Fullwidth ASCII (Ａ-Ｚ / ０-９) is Latin — read by the base voice, folded
    # to ASCII, not handed to a CJK voice (gripe 161851 #1).
    out = split_by_script(
        "モデル ＧＰＴ－４ です",
        base_voice="af_heart",
        base_lang="en-us",
        cjk_lang="ja",
        cjk_voice="jf_alpha",
    )
    base = [(t, v, ln) for t, v, ln in out if v == "af_heart"]
    assert base, out
    folded = " ".join(t for t, _v, _ln in base)
    assert "GPT-4" in folded  # ＧＰＴ－４ folded to ASCII


def test_split_by_script_routes_bopomofo_to_cjk():
    # Bopomofo (U+3100–312F) sat in the gap between the old kana and Han ranges
    # and leaked to the base espeak voice; it now routes to the CJK voice.
    out = split_by_script(
        "read ㄅㄆㄇ now",
        base_voice="af_heart",
        base_lang="en-us",
        cjk_voice="zf_xiaoxiao",
        cjk_lang="cmn",
    )
    assert any("ㄅㄆㄇ" in t and ln == "cmn" for t, _v, ln in out)


def test_render_narration_lang_only_meta_derives_voice():
    # End-to-end: a Chinese draft chunk sets only meta.cjk_lang='cmn'.
    store = _Store([_Chunk("paragraph", "hello 世界", {"cjk_lang": "cmn"})])
    segs = render_narration(
        store, _Ref(), default_voice="af_heart", default_lang="en-us"
    )
    triples = [(s.text, s.voice, s.lang) for s in segs]
    assert ("世界", "zf_xiaoxiao", "cmn") in triples


def test_markdown_segments_splits_mixed_block_natively():
    # A vocab-drill line: English prompt then the Japanese answer, one block.
    segs = markdown_segments("cat is ねこ", voice="af_heart", lang="en-us")
    langs = [(s.text, s.voice, s.lang) for s in segs]
    assert ("cat is", "af_heart", "en-us") in langs
    assert ("ねこ", "jf_alpha", "ja") in langs


def test_render_narration_splits_mixed_chunk_and_respects_meta_cjk():
    store = _Store(
        [
            _Chunk(
                "paragraph",
                "hello 世界",
                {"cjk_lang": "cmn", "cjk_voice": "zf_xiaoxiao"},
            ),
        ]
    )
    segs = render_narration(
        store, _Ref(), default_voice="af_heart", default_lang="en-us"
    )
    triples = [(s.text, s.voice, s.lang) for s in segs]
    assert ("hello", "af_heart", "en-us") in triples
    # Han-only 世界 follows the chunk's cjk meta override.
    assert ("世界", "zf_xiaoxiao", "cmn") in triples

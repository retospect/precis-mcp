# Mixed-script narration — speak Japanese inside an English cast

> **Status: shipped (v8.24.0).** All three slices landed: `split_by_script`,
> wiring into `render_narration` + `markdown_segments`, and the `precis-voice`
> / `precis-audio-help` updates (guarantee + vocab-drill recipe). Chosen
> defaults (per Reto): drill beat via block structure + existing pause (no new
> pause control); Han-only defaults to `ja`; script-splitting always-on.
> Additive + backward-compatible: a no-op for pure-single-script text, so every
> existing briefing / meditation / paper narration is byte-identical.

## Motivation

The Japanese-reading podcast cast wants to *speak Japanese*: a vocab drill of
English-prompt → Japanese-answer, and Japanese words quoted mid-sentence. Today
that fails — the listener hears an "unknown character" / garbled artifact where
the Japanese should be.

**Root cause.** The narration segmenter assigns **one** `(voice, lang)` to a
whole block:

- `render_narration` (draft path) — one segment per chunk, `lang` from
  `chunk.meta.lang` or the draft default.
- `markdown_segments` (prose/cast path) — one segment per markdown block, a
  single `(voice, lang)` for the entire cast.

So Japanese text inside an otherwise-English cast lands in an English segment
and is fed to the **espeak-en** engine (or misaki-en G2P), which has no reading
for kana/kanji → the "unknown character" symptom. The *renderer* is not the
problem: `precis.export.audio._stitch` already concatenates segments from
**different engines at different sample rates**, and `KokoroSynth` already
routes `lang='ja'` through the misaki `JAG2P` (`pyopenjtalk`). The only missing
piece is a segmenter that emits a `lang='ja'` segment for the Japanese span.

## Non-goals

- No new TTS engine, no renderer change (mixed-engine stitching already works).
- No migration, no new kind. Voice/lang routing is metadata + pure text ops.
- Not building a full language classifier. Script ranges (kana / Han) are
  enough; ambiguous Han-only runs use a documented default.

## Design

### 1. `split_by_script` — the one new primitive

A pure function in `precis/draft/narrate.py`:

```python
def split_by_script(
    text: str, *, base_voice: str, base_lang: str,
    cjk_voice: str = "jf_alpha", cjk_lang: str = "ja",
) -> list[tuple[str, str, str]]:
    """Split text into (text, voice, lang) spans on CJK boundaries.

    A contiguous run containing any kana (U+3040–30FF) — or, absent kana,
    CJK Han (U+3400–9FFF, U+F900–FAFF) — becomes a (cjk_voice, cjk_lang)
    span; everything else keeps (base_voice, base_lang). Adjacent same-lang
    spans coalesce. Pure-Latin text returns a single base span (no-op)."""
```

- **Kana ⇒ Japanese, unambiguously.** Hiragana/Katakana appear only in
  Japanese, so any run with kana routes to `ja` regardless of the `cjk_lang`
  default.
- **Han-only ⇒ `cjk_lang` default (`ja`).** Shared between zh/ja; the default
  is `ja` because that's the live need. A Chinese draft overrides via meta
  (`cjk_lang='cmn'`, `cjk_voice='zf_xiaoxiao'`); documented, not inferred.
- **Latin/space/punct between two CJK runs** stays with whichever side it
  abuts by the coalescing rule; ASCII punctuation adjacent to a CJK run is
  kept in the base span so English narration doesn't inherit stray glyphs.

### 2. Wire into both segment paths (always-on, no-op for non-CJK)

`render_narration` and `markdown_segments` currently append one
`NarrationSegment` per block. They instead run the block's speakable text
through `split_by_script` and append one segment per returned span (same
`kind`, so pause/heading behaviour is unchanged). Pure-English blocks yield
exactly one span → **identical output to today**.

CJK routing target is threaded from meta so a cast can pick the voice:

- draft: `chunk.meta.cjk_voice` / `chunk.meta.cjk_lang` (or draft-level
  default passed to `render_narration`).
- prose cast: `markdown_segments(..., cjk_voice=?, cjk_lang=?)`, defaulted to
  `jf_alpha` / `ja`.

### 3. The vocab-drill recipe (authoring convention, no new code)

With script-splitting, the drill needs no engine change — it is a
**writing pattern**, documented in `precis-voice`:

- Write each call-and-response turn as its own short block: the English prompt
  sentence, then the Japanese answer on its own line/block. The inter-segment
  pause is the beat; a heading-kind line gets the longer breath.
- Repeat the turn 2–3× as the author wants — each line is a segment.
- The Japanese answer auto-routes to `jf_alpha`/`ja` (native misaki output).

*(Open question A below: whether to add an explicit longer "answer gap" pause
control, or rely on block structure + the existing inter-segment pause.)*

### 4. Skill + docs

- `precis-voice`: new "Japanese and mixed-script narration" section (the
  script-split guarantee + the vocab-drill recipe) and a note on rule 7.
- `precis-audio-help`: one line that mixed-script segments split automatically.

## Slice plan

1. `split_by_script` + unit tests (pure, no TTS). *(Core — fixes the bug.)*
2. Wire into `render_narration` + `markdown_segments` + tests (no-op parity +
   mixed-script split).
3. `precis-voice` / `precis-audio-help` updates (recipe + guarantee).

## Open questions (for Reto)

- **A. Drill answer-gap:** rely on block structure + the existing 0.45 s
  inter-segment pause (proposal), or add an explicit longer "answer pause"
  (e.g. a blank-line/`…` convention → a `drill` segment kind with a ~1.5 s gap)
  so the listener has time to answer before the Japanese?
- **B. Han-only default lang:** default ambiguous Han-only runs to `ja`
  (proposal, matches the live cast) vs. require the cast to state it explicitly
  (safer for a future Chinese cast, but the current Japanese cast must then
  opt in or the bug persists silently).
- **C. Always-on vs opt-in:** proposal makes script-splitting always-on
  (no-op for non-CJK, so it can't regress English casts and it fixes the bug
  without the author remembering a flag). Opt-in would be more conservative but
  re-opens the silent-failure hole.

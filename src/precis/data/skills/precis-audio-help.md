---
id: precis-audio-help
title: precis — voice drafts (narrate a draft to audio) + the pronunciation lexicon
summary: audio is a cross-cutting EXPORT LAYER over any draft (not a kind); `precis draft audio <slug>` narrates via Kokoro TTS; per-chunk meta.voice/lang = a voice score; two-level pronunciation lexicon (personal + per-draft); publishes to the private podcast feed
applies-to: draft narration/audio export; chunk meta.voice/lang; meta.pronunciation; PRECIS_LEXICON_FILE; precis draft audio; precis podcast
status: active
---

# precis-audio-help — voice drafts + pronunciation

**Audio is a LAYER, not a kind.** Any draft (patent / techreport / paper)
narrates to audio the same way it exports to docx/pdf. The narration layer
reads a draft and never writes back.

## Narrate a draft

    precis draft audio <slug|id> [--voice af_heart] [--lang en-us]
                                 [--speed 1.0] [--max-segments N] [--publish]

Runs on a host with the `[tts]` extra + Kokoro model files + ffmpeg (the
inference node). `--publish` drops the episode on the private podcast feed
(`precis podcast add` / `/podcast/feed.xml`). `--max-segments` previews a long
draft. `speakable()` strips handles/citations/math/markdown for the ear; math
is currently spoken as "equation" (LaTeX→speech is backlogged).

## The voice score — who says each part

Per-chunk **`meta.voice`** / **`meta.lang`** override the `--voice`/`--lang`
defaults, so a draft narrates in mixed voices / languages: a paragraph in
`af_heart` (US), a quoted passage in `bf_emma` (UK, `en-gb`), a French epigraph
in `ff_siwis` (`fr`). Set them on the chunk's meta; nothing new to author in the
prose. Voices: `af_*`/`am_*` (US female/male), `bf_*`/`bm_*` (UK), plus other
languages. `--speed` (0.5–2.0) is the one native prosody knob.

## Pronunciation lexicon — how special words sound

A `{surface: respelling}` map, applied whole-word before TTS, so "precis",
"arXiv", names and jargon come out right. **Two levels** (per-draft wins over
personal):

- **Personal (cross-draft base)** — teach a word once, right in *every* draft.
  A JSON file at **`PRECIS_LEXICON_FILE`**:

      { "precis": "pray-see", "arXiv": "archive", "Reto Stamm": "REH-toh stam" }

- **Per-draft override** — like abbrevs, but a *free* lexicon (covers words that
  aren't glossary terms). Set **`meta.pronunciation`** on the draft ref:

      edit(kind='draft', id=<slug>, ...)   # meta.pronunciation = {"boxel": "BOX-ell"}

Write a **respelling** ("pray-see") — author-friendly; the narrator speaks it.
(Precise espeak/IPA phonemes are a future notation.) Unlike an abbreviation's
*expansion* (contextual, per-document), a *pronunciation* is stable across
documents — which is why the personal layer exists.

Not-yet: pronunciation on term/glossary entries; inline per-occurrence override
for homographs; LaTeX→speech. See `docs/design/audio-feed.md` + OPEN-ITEMS.

## Automatic producers

Drafts aren't the only thing that narrates. The **news briefing** publishes a
daily audio episode automatically: a worker pass (`briefing_audio`) on the TTS
host reads the persisted `briefing-<date>` ref, narrates its markdown (via
`narrate.markdown_segments` — the prose path, links→anchor text, headings pause),
and publishes to the same feed with `source="news"`. Enabled with
`PRECIS_BRIEFING_AUDIO_ENABLED=1` on spark; idempotent per briefing. Non-draft
producers reuse `export.audio.synthesize_text` (the shared stitch loop). Design +
how to add the next producer: `docs/design/audio-feed.md`.

## Consume

Subscribe **on-device** over Tailscale — Apple Podcasts *Add a Show by URL* or
Downcast → the feed URL. Server-mediated apps (Overcast / Pocket Casts) can't
reach a tailnet feed.

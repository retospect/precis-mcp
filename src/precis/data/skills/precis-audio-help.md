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

Each chunk carries **`meta.voice`** / **`meta.lang`** (overriding the
`--voice`/`--lang` defaults), so a draft narrates in mixed voices / languages: an
`af_heart` (US) body, a `bf_emma` (UK) quote, a `ff_siwis` French epigraph, a
`zf_xiaoxiao` Mandarin drill. **Set them per chunk** — first-class, validated:

    put(kind='draft', id='mydraft', chunk_kind='paragraph', text='你好世界',
        voice='zf_xiaoxiao', lang='cmn', at={'after': 'dc12'})
    edit(kind='draft', id='dc12', voice='ff_siwis', lang='fr-fr')   # retrofit an existing chunk

Give `voice` and/or `lang`; the other is inferred (a voice knows its language; a
language has a default voice). A typo fails loudly with a catalogue hint — they
must agree (an Italian voice can't speak French text). `--speed` (0.5–2.0) is the
one native prosody knob.

**Mixed scripts split automatically.** You don't need `meta.lang` to voice a
Japanese word inside an English chunk — the narrator splits each block by
script (`split_by_script`), routing kana/kanji runs to a Japanese voice
(`jf_alpha`) and keeping the English on its base voice. Kana ⇒ Japanese always;
Han-only defaults to Japanese, override with `meta.cjk_lang='cmn'` /
`meta.cjk_voice='zf_xiaoxiao'` for Mandarin. See `precis-voice` (the vocab-drill
recipe).

### The catalogue (Kokoro v1.0, 54 voices)

| lang code | voices (examples) | notes |
|---|---|---|
| `en-us` | `af_heart` (default), `am_michael`, `af_nova`, … (20) | |
| `en-gb` | `bf_emma`, `bm_george`, … (8) | |
| `fr-fr` | `ff_siwis` | **only one** French voice |
| `it` | `if_sara`, `im_nicola` | |
| `es` | `ef_dora`, `em_alex` | |
| `pt-br` | `pf_dora`, `pm_alex` | |
| `hi` | `hf_alpha`, `hm_omega` | |
| `cmn` | `zf_xiaoxiao`, `zm_yunxi`, … (8) | Chinese is **`cmn`**, not `zh` |
| `ja` | `jf_alpha`, `jm_kumo`, … (5) | |

**German is not available** in Kokoro v1.0. **cn/jp** phonemize but want the
misaki G2P for good output (a synth-side upgrade) — test by ear.

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

### Daily casts — morning brief + evening nidra

Two standing **casts** publish daily, each a `draft` composed then narrated
through the same path — two voice profiles over one spine:

- **`reading`** — the morning situational-awareness brief (voice `bm_george`,
  ~15 min). Producer `reading.briefing_cast.build_reading_briefing` unions lanes:
  the news wire (today's `briefing-<date>`, consumed not re-derived), overnight
  system activity, recall (Anki leeches + new concepts), and — as they land —
  reading (booklet) + quest activity.
- **`nidra`** — the evening concept-graph meditation (voice `af_nicole`, ~45 min).
  Producer `reading.meditation.build_meditation`, a segmented long-form walk.

Both casts **link their draft back to the sources they drew on** (a cast names
its sources but reads no URL aloud, so the link is the durable pointer): the
`reading` brief links papers/findings `cites`, the news wire `derived-from`, and
drafts/quests `related-to`; the `nidra` walk links each walked concept
`related-to`. So `links_for` on the cast draft reopens what it mentioned.

Both compose with a **nice model** (`claude-opus`) and persist a standalone dated
`draft` marked `meta.cast` (+ `meta.voice`). **TTS is a separate downstream step:**
the `cast_audio` pass on spark (`PRECIS_CAST_AUDIO_ENABLED=1` + `PRECIS_TTS_IMAGE`)
narrates any un-narrated cast draft via `render_narration` → `render_episode` →
the feed (`source="reading"`), idempotent on `meta.audio_episode_id`. Compose runs
as the `reading_brief` / `meditation` **`claude_inproc`** job_types on a daily
`level:recurring` watch — on melchior, where the litellm proxy serving `claude-opus`
lives (same host as the news briefing); the compose and the narration never block
each other.

    precis cast run reading            # compose today's morning-brief draft now
    precis cast run nidra --publish    # compose + narrate + publish (on spark)
    precis cast schedule --now         # install the daily watches + cast both now

## Consume

Subscribe **on-device** over Tailscale — Apple Podcasts *Add a Show by URL* or
Downcast → the feed URL. Server-mediated apps (Overcast / Pocket Casts) can't
reach a tailnet feed.

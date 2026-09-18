---
id: precis-audio-help
title: precis — voice drafts (narrate a draft to audio) + the pronunciation lexicon
summary: audio is a cross-cutting EXPORT LAYER over any draft (not a kind); per-chunk meta.voice/lang = a voice score; two-level pronunciation lexicon (personal + per-draft); publishes to the private podcast feed
answers:
  - how do I narrate a draft to audio?
  - how do I control who speaks each part / the voice score?
  - how do I fix the pronunciation of a special word or acronym?
  - where does a narrated draft end up — how do I listen to it?
applies-to: draft narration/audio export; chunk meta.voice/lang; meta.pronunciation
status: active
tags: [drafting]
kinds: [draft]
---

# precis-audio-help — voice drafts + pronunciation

**Audio is a LAYER, not a kind.** Any draft (patent / techreport / paper)
narrates to audio the same way it exports to docx/pdf — a host-side pass
reads the draft's chunks and publishes an episode to the podcast feed; the
narration layer never writes back to the draft. `speakable()` strips
handles/citations/math/markdown for the ear; math is currently spoken as
"equation" (LaTeX→speech is a future notation).

## The voice score — who says each part

Each chunk carries **`meta.voice`** / **`meta.lang`**, so a draft narrates in
mixed voices / languages: an `af_heart` (US) body, a `bf_emma` (UK) quote, a
`ff_siwis` French epigraph, a `zf_xiaoxiao` Mandarin drill. **Set them per
chunk** — first-class, validated:

```python
put(kind='draft', id='mydraft', chunk_kind='paragraph', text='你好世界',
    voice='zf_xiaoxiao', lang='cmn', at={'after': 'dc12'})
edit(kind='draft', id='dc12', voice='ff_siwis', lang='fr-fr')   # retrofit an existing chunk
```

Give `voice` and/or `lang`; the other is inferred (a voice knows its language; a
language has a default voice). A typo fails loudly with a catalogue hint — they
must agree (an Italian voice can't speak French text).

**Mixed scripts split automatically.** You don't need `meta.lang` to voice a
Japanese word inside an English chunk — the narrator splits each block by
script, routing kana/kanji runs to a Japanese voice (`jf_alpha`) and keeping
the English on its base voice. Kana ⇒ Japanese always; Han-only defaults to
Japanese, override with `meta.cjk_lang='cmn'` / `meta.cjk_voice='zf_xiaoxiao'`
for Mandarin. See `precis-voice` (the vocab-drill recipe).

### The catalogue (54 voices)

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

**German is not available.**

## Pronunciation lexicon — how special words sound

A `{surface: respelling}` map, applied whole-word before TTS, so "precis",
"arXiv", names and jargon come out right. **Two levels** (per-draft wins over
a personal, cross-draft base maintained by the deployment). Set the per-draft
override — a *free* lexicon covering words that aren't glossary terms — at
creation or any time after:

```python
put(kind='draft', id='mydraft', title='…', project=<todo_id>,
    meta={'pronunciation': {'boxel': 'BOX-ell'}})
edit(kind='draft', id='mydraft',
    meta={'pronunciation': {'boxel': 'BOX-ell', 'precis': 'PRAY-see'}})
```

`edit`'s `meta=` here replaces the **whole** `pronunciation` dict (it isn't
merged entry-by-entry) — pass every entry you want kept; `meta={'pronunciation':
None}` clears it. `id=` must be the draft's slug (or ref id), not a `dc<id>`
chunk handle — that addresses a *chunk's* metadata instead (a registry term's
attribute bag), a different op.

Write a **respelling** ("pray-see") — author-friendly; the narrator speaks
it. Unlike an abbreviation's *expansion* (contextual, per-document), a
*pronunciation* is stable across documents, which is why the personal
cross-draft base exists alongside the per-draft one.

## Consume

Subscribe **on-device** over Tailscale — Apple Podcasts *Add a Show by URL*
or Downcast → the feed URL. Server-mediated apps (Overcast / Pocket Casts)
can't reach the feed.

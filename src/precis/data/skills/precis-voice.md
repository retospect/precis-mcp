---
id: precis-voice
title: precis — authoring drafts for the ear (audio narration)
summary: how to write a draft that narrates well as spoken audio — describe relationships not formulas, avoid slashes and backslashes, keep prose clean and lexicon the hard words; plus the morning-brief and evening-meditation (nidra) voice profiles
applies-to: draft narration (render_narration / export_audio, kind='draft')
status: active
---

# precis-voice — writing for the ear

Any `draft` can be narrated to audio: `render_narration` walks it in reading
order into a *voice score*, and `export_audio` drives a TTS engine (Kokoro) from
that. This skill is the **craft** — how to author a draft that sounds good spoken,
not one that merely reads well on the page. Reference for the mechanism lives in
`precis/draft/narrate.py`.

## How do I make an audio draft?
## Write a draft to be spoken / narrated
## Make a morning brief or an evening meditation

Write flowing prose and follow the rules below. The narrator **strips markup** so
the ear gets clean text — which means anything that only makes sense *visually*
is lost. Author accordingly.

### 1. Describe relationships — never write formulas

Math is **dropped** for the ear: `$…$` inline math vanishes, `$$…$$` display math
becomes a flat spoken "equation" cue. A formula narrates as nothing, or as noise.
So say the relationship **in words**:

- not `$E \propto v^2$` → "the energy grows with the *square* of the speed"
- not `$O(n \log n)$` → "the cost grows a little faster than the number of items"
- not `p = nRT/V` → "the pressure rises with temperature and falls as the volume
  grows"

Words carry the meaning the symbol would have shown. This is the single biggest
difference between a readable draft and a speakable one.

### 2. Expand abbreviations and symbols into words

Written shorthand reads badly aloud — spell it out:

- `e.g.` → "for example", `i.e.` → "that is", `etc.` → "and so on", `vs.` →
  "versus", `cf.` → "compare", `approx.` → "roughly".
- `Fig. 3` → "figure three", `Sec. 2` → "section two", `Dr.`/`Prof.` →
  "Doctor"/"Professor", `No.` → "number".
- symbols: `&` → "and", `%` → "percent", `#` → "number", `@` → "at", `±` →
  "plus or minus", `→` → "leads to", `≈` → "about".
- **Acronyms** read as letters. That's fine when you want it ("R N A"), but if an
  acronym should be spoken as a word or expanded, write the expansion or give it a
  lexicon respelling (rule 5) — don't leave the ear guessing.

### 3. Avoid slashes and backslashes

They read as "slash" / "backslash" or leave artifacts, and paths and URLs are
unspeakable. Rewrite:

- "and/or" → "or"
- "input/output" → "input and output"
- a file path or URL → describe it ("the config file", "the project page"), don't
  voice it
- a backslash line break or LaTeX crumb → delete it; write the sentence out

### 4. Flowing prose only — no lists, tables, code, figures

The narrator **skips** bullet and numbered lists, tables, code blocks, figures,
and glossary `term` chunks (they don't read aloud as prose). If it matters for the
ear, write it as sentences. Turn a three-bullet list into "There are three parts.
First… Then… Finally…".

### 5. Keep prose clean; fix hard words with the lexicon

Don't spell out or phonetically mangle jargon *inline* — it clutters the text and
breaks on the page. Instead use the **pronunciation lexicon** (`surface →
respelling`), applied out of band: names, acronyms, and jargon like `precis`,
`arXiv`, `pgvector` get a respelling there while the prose stays clean. Spell out
an acronym in words the first time only if the ear needs it.

### 6. Pace with sentence length and structure

Short sentences and full stops slow the voice down; a heading gets a longer
leading breath. Numbers and units: write them speakable ("about three thousand",
"twelve millivolts"), not as raw digit-and-symbol strings.

### 7. Per-chunk voice and language

A chunk's `meta.voice` / `meta.lang` overrides the draft default — use it for a
quoted passage in a second voice, or a foreign phrase in its own language, so the
engine pronounces it correctly.

## The two standing profiles

Both are `draft`s composed as a graph walk (reading-prep loop) and rendered by the
voice layer. They differ in **voice, pacing, and intent**.

### Morning brief — voice `bm_george` (British)

Clear-mind priming: what's new, what's due, what's live — the things you want to
meet with a fresh head. **Crisp, present, forward-looking.** Full sentences, brisk
pace, a light intention at the end. Describe today's new concepts and their
relationships in plain words (rule 1). Energising, not exhaustive — it's a ~15
minute orientation, with the detail a tap away.

### Evening meditation (nidra) — voice `af_nicole`

A calming, hypnagogic walk through the graph for the edge of sleep. **Slow, warm,
second-person, quiet.** Structure:

- **Induction** (a familiar, retained opening): a few settling breaths, letting
  the day set down.
- **The walk**: drift gently between *familiar, well-known* concepts along their
  closest connections — no jarring jumps, no new hard material, and **no recall
  prompts of any kind** (this is exposure, not testing). Describe relationships
  softly and *mostly* correctly — soothing framing is welcome, **but never state
  something false**; the precise version lives in the cards.
- **Coda** (retained): a consistent fade — sentences shorten, pauses lengthen, the
  content softens and dissolves as you drift.

It **alters daily but retains elements**: the induction, the coda, and a few
anchor concepts recur (familiar, like a half-known bedtime story); the path
between them varies night to night. Keep every rule above, but lean especially on
**relationships-in-words** and gentle pacing — the goal is calm, not rigor.

## From draft to episode

A draft you author this way becomes audio through the shipped pipeline (no need
to re-roll any of it): `render_narration` turns the draft into a voice score,
`export_audio` drives the TTS synth (`precis.tts.kokoro.KokoroSynth`), and
`audio_feed.publish_episode(...)` (or `precis podcast add`) puts the episode on
the private feed. Delivery + on-device playback are already solved — see
`docs/design/audio-feed.md`. Set the draft's default voice per profile
(`bm_george` for the morning brief, `af_nicole` for the evening meditation).

## See also

This skill is the **craft** (how to write speakable prose + the standing
profiles). For the **mechanism** — audio as a cross-cutting export layer, the
`precis draft audio` CLI, per-chunk `meta.voice`/`meta.lang`, and the two-level
pronunciation lexicon — see `precis-audio-help`.

```python
get(kind='skill', id='precis-audio-help')  # the mechanism: narrate a draft, lexicon, feed
get(kind='skill', id='precis-cloze')       # authoring recall cards (the active complement)
get(kind='skill', id='precis-overview')    # kinds + skills index
```

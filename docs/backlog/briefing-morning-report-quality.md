---
status: idea
pillar: personal
title: Morning briefing — misleading stat, a multi-ask to split, and text the TTS mispronounces
---

# Morning report quality

Bundled in the 2026-10-11 gripe triage. Owners: `src/precis/workers/briefing.py`,
`src/precis/workers/briefing_audio.py`, `src/precis/tts/`.

## 1. "Papers touched" counts robot reads (gr434185)

The stat includes reads by workers and agents, so it reads as human effort
it is not. Fix direction: split into human-touched versus automated, or
document the definition in the report itself.

Who: needs-Reto: split the number or only label it.

## 2. Send the morning report 2 h earlier (gr415941, part 1)

Reto ruled 2026-10-11 to split gr415941 into five items (sections 2–6). Shift
the report's schedule 2 h earlier.

Who: agent-code (briefing worker). S.

## 3. Paper-cluster links in the report body (gr415941, part 2)

Link paper clusters (e.g. the azobenzene/photoswitch thread) in the body of
the report.

Open question: does each scientific fact need its own finding hub, or can
a cluster link stand without one?

Who: agent-code (briefing worker); needs-Reto on the open question.

## 4. Chemistry pronunciation in report audio (gr415941, part 3)

Pronounce chemical names correctly in the report audio, via the phonetics
DB. It shares its lexicon with the TTS preprocessing in section 7.

Who: agent-code (`src/precis/tts/`).

## 5. Are backlog cost-accounting ticks an expensive token burn? (gr415941, part 4)

Investigate whether the backlog cost-accounting ticks cost real tokens for
little value. Output is a measurement, then keep, thin or drop.

Who: agent-code (budget worker).

## 6. DFT lint rejects azobenzene linear-motor quests as "not a slab" (gr415941, part 5)

Moved to [dft-lint-per-quest-type.md](dft-lint-per-quest-type.md).

## 7. TTS reads raw text (gr415909)

`src/precis/tts/` has no text preprocessing, so the voice says "£2bn"
literally, spells abbreviations wrongly, and the temporal framing ("today",
"last week") is wrong for an episode listened to later. Fix direction: a
normalisation step ahead of `render`: currency and magnitude expansion
("£2bn" to "2 billion pounds"), an abbreviation lexicon, and temporal
phrases anchored to the episode date. Share the lexicon with the phonetics
DB of section 4. See also the TTS-normalisation question in
[cast-followups.md](cast-followups.md); decide there whether casts
author TTS-friendly text or filter afterwards before building this.

Who: agent-code for the preprocessor; needs-Reto: filter-after versus
author-friendly (the open cast-followups question).

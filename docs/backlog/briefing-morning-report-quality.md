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

## 2. A five-part ask filed as one gripe (gr415941)

The gripe bundles: shift the schedule 2 h earlier; paper-cluster links in
the report body; a phonetics DB for TTS; a cost-tick review; a DFT lint per
quest type. These are separate items with different owners.

Who: needs-Reto to split. Suggested cut: schedule and cluster links (briefing
worker), phonetics DB (belongs with item 3 below), cost-tick review (budget
worker), DFT lint (quests pillar). Each then becomes its own backlog item.

## 3. TTS reads raw text (gr415909)

`src/precis/tts/` has no text preprocessing, so the voice says "£2bn"
literally, spells abbreviations wrongly, and the temporal framing ("today",
"last week") is wrong for an episode listened to later. Fix direction: a
normalisation step ahead of `render`: currency and magnitude expansion
("£2bn" to "2 billion pounds"), an abbreviation lexicon, and temporal
phrases anchored to the episode date. Share the lexicon with the phonetics
DB of item 2. See also the TTS-normalisation question in
[cast-followups.md](cast-followups.md); decide there whether casts
author TTS-friendly text or filter afterwards before building this.

Who: agent-code for the preprocessor; needs-Reto: filter-after versus
author-friendly (the open cast-followups question).

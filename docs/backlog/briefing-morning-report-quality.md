---
status: idea
pillar: personal
title: Morning briefing — a multi-ask to split and text the TTS mispronounces
---

# Morning report quality

Bundled in the 2026-10-11 gripe triage. Owners: `src/precis/workers/briefing.py`,
`src/precis/workers/briefing_audio.py`, `src/precis/tts/`.

## 1. Send the morning report 2 h earlier (gr415941, part 1)

Reto ruled 2026-10-11 to split gr415941 into five items (sections 1–5). Shift
the report's schedule 2 h earlier.

Who: agent-code (briefing worker). S.

## 2. Paper-cluster links in the report body (gr415941, part 2)

Link paper clusters (e.g. the azobenzene/photoswitch thread) in the body of
the report.

Open question: does each scientific fact need its own finding hub, or can
a cluster link stand without one?

Who: agent-code (briefing worker); needs-Reto on the open question.

## 3. Chemistry pronunciation in report audio (gr415941, part 3)

Pronounce chemical names correctly in the report audio, via the phonetics
DB. It shares its lexicon with the TTS preprocessing in section 6.

Who: agent-code (`src/precis/tts/`).

## 4. Are backlog cost-accounting ticks an expensive token burn? (gr415941, part 4)

Investigate whether the backlog cost-accounting ticks cost real tokens for
little value. Output is a measurement, then keep, thin or drop.

Who: agent-code (budget worker).

## 5. DFT lint rejects azobenzene linear-motor quests as "not a slab" (gr415941, part 5)

Moved to [dft-lint-per-quest-type.md](dft-lint-per-quest-type.md).

## 6. TTS reads raw text (gr415909)

Currency and magnitude expansion ("£2bn" to "two billion pounds") shipped in
`src/precis/draft/verbalize.py`. Still open: an abbreviation lexicon
(letters versus word versus table; gr415909 says discuss first), temporal
phrases anchored to the episode date ("today", "last week" are wrong for an
episode listened to later), and annotation (pronunciation/emphasis hint)
preprocessing. Share the lexicon with the phonetics DB of section 3. See also
the TTS-normalisation question in [cast-followups.md](cast-followups.md);
decide there whether casts author TTS-friendly text or filter afterwards
before building more.

Who: agent-code for the preprocessor; needs-Reto: filter-after versus
author-friendly (the open cast-followups question), and the abbreviation
reading policy.

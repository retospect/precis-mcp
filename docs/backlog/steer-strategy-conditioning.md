---
status: idea
title: STEER-style natural-language strategy conditioning for quest proposals and routes
---

# Strategy conditioning (steal from STEER/SynthEx)

Steal identified by the capability-landscape comparison (draft
`capability-landscape`, 2026-08): Schwaller's STEER (Matter 2026, corpus
pa4715) has the chemist state a synthesis *strategy in natural language*;
an LLM scores candidate routes against that strategy and explains itself.
SynthEx (pa259454) extends to multi-agent template-free route design.

The precis analogue: quests already let the discovery agent own all
chemistry, but Reto's steer today is editing the quest body. A first-class
*strategy statement* on a quest — "prefer earth-abundant dopants",
"avoid subsurface modifications, they're synthetically implausible" —
that the proposal step must score candidates against (and explain
deviations from) would give the operator a steering wheel that survives
tick resets and shows up in the dossier. Same pattern applies to the
`route` kind when it wakes: strategy-aware ranking over AiZynthFinder/
ASKCOS output instead of raw route scores.

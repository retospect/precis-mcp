---
status: idea
title: ChemBench as an external chemistry yardstick inside llm_eval
---

# ChemBench in the golden-eval harness

Steal identified by the capability-landscape comparison (draft
`capability-landscape`, 2026-08): ChemBench (Nature Chemistry 2025, corpus
pa2708) benchmarks LLM chemical knowledge/reasoning against practicing
chemists; open tooling, widely reported.

precis's `llm_eval` measures candidate models on precis's *own* tasks
(model selection, not public benchmarking). Adding a ChemBench slice gives
the placement chains an external chemistry-competence axis: when choosing
which model rung runs quest ticks, frontier reviews, or taproot grounding,
"how much chemistry does this model actually know" is currently vibes.
Cheap first slice: run the published harness against the 3–4 models that
sit on the BIG/FRONTIER chains, store scores as `llm` catalog capability
axes. Related: ChemPile (pa259457) as eval/finetune corpus material — note
only, no commitment.

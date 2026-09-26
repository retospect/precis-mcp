---
status: draft
---

# Capability landscape steals

Grouped 2026-09-26 from 5 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## ChemBench in the golden-eval harness

_Grouped 2026-09-26; was `chembench-model-eval`, status idea._

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

## Categorizer rule distillation

_Grouped 2026-09-26; was `categorizer-rule-distillation`, status idea._

Steal identified by the capability-landscape comparison (draft
`capability-landscape`, 2026-08): Schwaller's ReactionClassifier
(arXiv:2607.01061, corpus pa53956) has agents *write and self-verify
deterministic classification rules*, then distills them into a lightweight
classifier covering 97.7% of unseen cases — LLM judgment spent once at
rule-authoring time, not per-item.

precis's classifier axes (ROLE3, TAPROOT, patent_example, domain/scale/…)
score every chunk with per-chunk LLM calls. For axes whose decisions are
substantially pattern-like (section-path cues, citation-marker shapes,
tense-of-performance), the same two-stage pattern applies: have an agent
propose explicit rules, verify them against the existing labeled corpus
(we have millions of scored chunks as ground truth), and run the cheap
rule tier first with LLM fallback only on low-confidence. Payoff is
classifier cost and reclassification speed when an axis version bumps.

## Strategy conditioning (steal from STEER/SynthEx)

_Grouped 2026-09-26; was `steer-strategy-conditioning`, status idea._

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

## Tier-ladder promotion policy vs MFBO best practices

_Grouped 2026-09-26; was `tier-ladder-mfbo-policy`, status idea._

Steal identified by the capability-landscape comparison (draft
`capability-landscape`, 2026-08): "Best practices for multi-fidelity
Bayesian optimization in materials and molecular research" (Nature Comp.
Sci. 2025, corpus pa259461) answers exactly the question the quest tier
ladder (screening → NEB-with-parking → co-adsorbed verify) hard-codes:
*when does paying for higher fidelity actually improve the search, and
when does it waste budget?*

Today promotion is capped-count and code-driven. Reading the paper's
findings against qu164903's own history (1,156 results, known
screening-vs-NEB rank inversions like the rejected Ta/Cd leaders) would
either validate the current caps or yield a cheap acquisition-style rule
for who gets promoted. Read-and-design item first — no code until the
paper's body lands and someone checks the regimes match (their cost ratios
vs ours).

## BEAST DB import adapter

_Grouped 2026-09-26; was `beast-db-import-adapter`, status idea._

Steal identified by the capability-landscape comparison (draft
`capability-landscape`, 2026-08): BEAST DB (beast-echem.org/beastdb,
paper doi:10.1021/acs.jpcc.4c06826, corpus pa254046) is a grand-canonical
DFT database of electrocatalyst properties — HER/OER/CO2R/**NRR** on 2,000+
catalysts, with explicit applied-potential and continuum-solvation effects.

Why it matters here: the NO→NH3 quest (qu164903) applies potential via the
closed-form CHE lever over MACE energies; BEAST DB carries *actual* GC-DFT
potential-dependent energetics for the same reaction family. As an external
evidence source it can sanity-check (or seed) catpath explorations at a
fidelity the quest never buys itself.

Shape: one more adapter in the ADR 0053 registry (`raw_record → (Scene,
ExternalRun, ExternalId)`), same rules as Catalysis-Hub — external runs
never serve compute cache hits, external designs refuse edit. Open
questions: bulk download format/licence (JPCC SI vs site API), and whether
per-potential rows map onto one ExternalRun or a family.

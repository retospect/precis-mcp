---
status: idea
title: Verifiable-rule distillation for classifier axes (steal from ReactionClassifier)
---

# Categorizer rule distillation

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

---
status: idea
pillar: platform
title: Model qualification — a model, prompt, skill or MCP change runs a fixed eval set against the incumbent before it takes live traffic
---

# Model qualification — a model, prompt, skill or MCP change runs a fixed eval set against the incumbent before it takes live traffic

Reto, 2026-10-01: a pipeline for eval and continuous improvement of
model / LLM / MCP / skills, plus a qualification step.

## What

A change to any of {model, prompt, skill text, MCP surface} that can alter
agent or pipeline output runs a fixed eval set against the incumbent and
ends in a **promote / reject** verdict before it takes live traffic.
Continuous improvement is the same loop run on a schedule against
candidates.

## Builds on (cite, do not duplicate)

- `eval-run-spine.md` — versioned runs, sequence numbers, the verdict column
  this item's promote/reject writes into.
- `llm-judge-reliability.md` — judges are instruments; qualification needs
  their error bars before a judge verdict can gate.
- `context-quality-eval.md` — the catalog + rubric of context-assembly evals.
- `curation-gate.md` — the reviewer loop that waits on the verdict column.

## Open

- The fixed eval set per change class (summarise, classify, extraction,
  skill, MCP surface) and who owns each.
- Promotion mechanics: who flips the `app_settings` chain row, and how a
  rejected candidate is recorded.

## First customer

`local-summarizer.md`: a local model replaces cloud `glm-4.7-flash` only
after passing this step.

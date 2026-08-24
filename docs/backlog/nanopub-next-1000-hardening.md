---
status: ready
title: "nanopub pipeline hardening for the next 1000 pubs — LLM provenance auto-flow, quote policy in the skill"
model: opus
---

# Harden the mint→publish process before volume publishing

Context: the first end-to-end nanopub (fi211520, publish row 28, artifact 2,
`RAtg6YcXNbGP-3Xc5l_9sE-ILEglIZIPKy9gzyaUCZDy8`, published 2026-08-24)
proved the ladder but exposed three process gaps that will recur on every
future pub. User decisions are already made (2026-08-24 session); this item
is implementation, not design.

## 1. LLM provenance auto-flow (decision: refuse-to-sign)

Today `precis nanopub sign --llm-model <id>` is a manual, forgettable flag;
artifact 2 shipped with no LLM attribution although an LLM authored the
claim sentence and payload.

- Payload envelope grows an optional `llm_models: [<model-id>, …]` list.
  Agents preparing a payload (hypothesis door parking, any agent-built
  approve payload) record the extracting/authoring model id there.
- `mint.approve` freezes it into `grounding` like every other envelope key;
  `mint.sign` folds `grounding.llm_models` into `_software_provenance()`
  (`ns1:llmModel` triples via `assemble.py` — mechanism already exists for
  the CLI flag). `--llm-model` stays as an additive override.
- **Gate (user decision): sign REFUSES an agent-prepared payload with no
  `llm_models`.** "Agent-prepared" = payload arrived via the parking door
  (`META_PROPOSED_PAYLOAD`) — a human-typed payload on the web form stays
  exempt. Wire as a mint gate so `view='mint-preflight'` reports it too.
- Update `src/precis/data/skills/` nanopub/mint skills so cluster agents
  know to set it.

## 2. Grounding-quote policy → write into the mint skill (decision: minimal covering span)

Decision: the standard is ONE contiguous quote — the minimal single-chunk
span that contains every structured field (material, quantity, …) —
falling back to multiple tight quotes only when no single-chunk span
covers them. Rationale: a single verbatim passage that states the whole
claim beats leanness; fi211520's two-tight-quotes style is the fallback,
not the default.

- Encode in the mint/nanopub skills (`src/precis/data/skills/`) where the
  quote-mechanics rules live ("trimmed to the bare assertion" needs
  rewording to reflect the covering-span default).
- Optional: advisory lint at preflight when passages > 1 but a covering
  span exists (cheap to detect: all field values in one chunk).

## Non-goals

- No supersede of fi211520 — user explicitly doesn't care about that one
  artifact, only the process.
- Single hard-coded OTS calendar stays (documented design choice; timeout
  shipped a2175668). OTS batch 1 polls finney (persisted calendar_url).

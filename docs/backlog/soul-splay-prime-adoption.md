---
status: idea
title: Soul store (splay promotion + budgeted startup priming) — deferred, adopt on mac dogfood evidence
prio: low
---

# Soul store: splay promotion + soul_prime — deferred

Decision session 2026-09-16 (Reto + agent), tranquil-fluttering-waffle
worktree. **Verdict: do not build now.** Revisit when the adoption
criteria below are met.

## What was proposed

A "soul store" prototyped by the agents in
<https://github.com/jordanhubbard/mac> (PR #814, open/unmerged as of
2026-09-16: `soul_graph.py` engine + `soul_mcp.py` server +
`soul_seed.py` startup gate, single PR despite the stack description;
`natasha/soul-mcp*` and `natasha/soul-prime` branches overlap it).
Three traversals over one node graph — splay (access promotes,
forgetting is structural ordering, pinned axioms at infinity), DAG
causal edges, tag cuts — plus RAG over the same nodes, and a
`soul_prime` startup tool: read the splay root + one discovery hop
seeded by session context, hard ~300-token budget, silent when context
novelty is below threshold.

## Mapping onto precis — most of it already exists

| Soul-store piece | Precis today |
|---|---|
| DAG causal edges | typed `link` between refs |
| Tag sideways cuts | tag axis on `memory` |
| RAG over the same nodes | hybrid search + embeddings |
| Pinned axioms | asa preamble tier 3: sticky memories, pinned with expiry |
| Per-agent filtering | per-user memory tag key (`author_handle` pattern) |
| Splay access-promotion | **missing** |
| Budgeted novelty-gated startup priming | **missing** |

So an adoption would port the two missing *behaviors* onto the memory
kind — a promotion score on refs plus a prime view / fifth preamble
tier — never vendor the parallel store (duplicate embeddings, duplicate
tags, two places a fact can live).

## Why deferred

1. **No demonstrated pain.** The 4-tier asa preamble
   (`src/asa_bot/preamble.py`) already varies per turn; no gripe exists
   whose root cause is context-selection failure the tiers can't
   express. Architecture-driven, not incident-driven.
2. **The static SOUL file is the fallback layer.** asa degrades to
   SOUL-only when the preamble build fails; making identity a prod-DB
   read removes the last dependency-free layer. Any adoption keeps the
   file as degraded mode, capping the upside.
3. **Access-promotion is a feedback loop with a silent failure mode.**
   Heat measures traffic, not importance; the busiest channel's themes
   dominate while a rarely-touched critical memory sinks invisibly.
   Current design routes importance through judgment (sticky pins,
   dream/consolidation pass), which is auditable; splay replaces
   judgment with usage stats.
4. **The novelty gate is unevaluable without an A/B harness.** A
   false-familiar (needed injection, gate stayed silent) surfaces in no
   log. The mac fleet is a purpose-built dogfood environment running
   this now — that evidence is free; adopting first buys the risk
   without the data.
5. **Promotion-on-access turns reads into prod writes** (agent_rw),
   against the deliberate-write convention. Solvable, but more moving
   parts for an unproven benefit.

## Adoption criteria (any two of three → revisit)

- (a) mac dogfood shows a high silence rate on familiar contexts with
  no caught false-familiar;
- (b) a handful of injections a static preamble demonstrably wouldn't
  have produced;
- (c) an asa-side gripe whose root cause is context-selection failure
  the current preamble tiers cannot express.

If adopted: key promotion heat per agent tag (one DAG, N orderings) —
shared heat across agents lets whoever ran last reshape everyone's
root. Pinning is also per agent tag: shared vs per-agent axioms are
different souls.

## Split out (independent, worth doing on its own merits)

Per-agent tag-scoped sticky memories —
`agent-tag-scoped-sticky-memories.md`. Cheap, judgment-preserving, no
new mechanics.

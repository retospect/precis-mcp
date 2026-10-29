---
status: draft
title: Block navigation — small addressable blocks, budgets instead of more
pillar: platform
prio: high
---

# Block navigation — small addressable blocks, budgets instead of `more`

Reto 2026-10-28: "For MCP, more is a kind of admission of failure. Skills
and other designed content should be hierarchical and small, with xrefs to
other sections. No page in skills should ever have more. Other content
only rarely if ever should have more — we should be able to pinpoint the
blocks or sets of blocks that we want." Reviewed the same day by Fable
(concept) and a Sonnet code-view pass; the rulings below are Reto's
responses to that review. Thread: `threads/agent-surface.md`.

Related: `mcp-seen-state-dedup.md` (stubs for blocks already sent),
`server-side-session-context.md` and `context-memory-hierarchy.md`
(preload), `tool-description-budgets.md` (cold-start bytes).

## Why

A `more` page costs a round trip and stays in context for the rest of the
run, so a drained 30 KiB skill is the worst case: every byte paid, nothing
skippable. The saving from blocks is mostly residency — a token read
early is re-billed (at cache-read price) on every later turn and dilutes
attention — so it grows with session length. Navigation wins when a
whole read would carry more than ~1–2k unneeded tokens in a long session
(~5k in a short one). The deciding parameter is the wrong-branch rate,
which TOC line quality sets, not block size.

## Rulings (Reto 2026-10-28)

- **Search stays the way in.** Retrieval lands on a block at any depth
  (collapsed-tree, as RAPTOR found better than top-down traversal). The
  tree is for addressing and context, not the route.
- **Hard rule for blocks, not responses.** Designed blocks: target
  500–2000 tokens, hard cap ~4k, enforced by an authoring lint. Responses
  take `budget=`; over budget the server refuses with an outline of block
  ids instead of paginating. `more` survives only for straight-through
  reads of ingested content, as skippable offset ranges.
- **Tree for identity, web for links.** One canonical parent per block
  (breadcrumb, ~30 tokens); typed links `requires` / `see-also` /
  `detail-of`, each with a line saying what is behind it.
- **Preload is per problem type, not static.** We know what we ask the
  agent to do, so it starts with one of n starter packs: a few indexes
  plus the blocks most used for that kind of task. A job launch knows its
  type; a free-form prompt goes through a categorizer. Packs are A/B
  tested. A small fixed set of packs keeps each pack a cacheable prefix
  (n prefixes, not one per session).
- **History never accumulates: each turn gets compacted content.** The
  asa bot already works this way: every Discord message goes to a fresh
  agent seeded with a summary of the prior channel activity, so no
  compaction ever runs. Generalise it: stable prefix (system + pack),
  then a rolling summary, then the new request. The prefix stays cached;
  only the summary and request are written per turn. Within one long
  run, fold old tool results to stubs (`mcp-seen-state-dedup.md`,
  gr499039). The fresh-agent pattern pays when summary + re-fetch costs
  less than carrying the history to the end of the run.
- **Ingested content gets a derived hierarchy** through `view='toc'`
  (papers already have one: `handlers/_paper_toc.py`), generalised across
  kinds rather than a new kind.
- **Build:** telemetry, batched `get(id=[...])`, search over block
  summaries, the authoring lint, an acceptance test, and an optimizer.

## What exists (code view, 2026-10-28)

- Pagination: `_pagination.py` splits at `DEFAULT_MAX_BODY_BYTES` (24 KiB)
  on section boundaries, folds weak search tails at 4 KiB, carries an
  `alt_hint`; it always paginates, never refuses.
- Skills: `handlers/skill.py` serves one file per slug; `slug~N` is a flat
  chunk index (no parent, no summary); `slug/toc` lists sections;
  `have=` returns an unchanged stub. `tests/test_skill_size.py` fails at
  32 KiB — above the split point, so it allows skills that need `more`.
  18 skill files exceed 24 KiB; the largest five sit at ~32 KiB.
- md kind: heading outline and section selectors (`handlers/md.py`).
- No `budget=` anywhere; `tests/test_token_budget.py` pins cold-start
  bytes only.
- Batched get: the `get` signature accepts a list; gripes implement it
  (`get(kind='gripe', id=[<ids>], view='body'|'full')`), no other kind
  does.
- Telemetry: the `tool_calls` ledger (`tool_ledger.record_call`) logs
  verb, kind, outcome, latency; no response bytes, no per-session
  roll-up, `result_count` unpopulated.

## Evidence (lit search 2026-10-28)

Perplexity survey (`perplexity-research:529881`, `529960`, `529961`,
`529965`, `529968`, `529969`, `529970`) plus 31 papers imported
(`pa529971`–`pa530008`). Figures are paper-reported.

- **Search-first holds.** RAPTOR's collapsed tree beat top-down
  traversal on QASPER at ~2k retrieved tokens (`pa529971`); HIRO's
  adaptive traversal used fewer tokens than collapsed but was no more
  accurate (`pa529972`). Retrieval lands at any depth; the tree is for
  addressing, not the route.
- **Wrong-branch rate is the deciding parameter.** Corpus2Skill traced
  19 of 45 doc-navigation failures to a wrong top-level branch
  (`pa530005`); RIT-RAG recovers 6.8–11.4 points by switching subtrees
  (`pa530006`). TOC-line quality sets the ceiling; breadcrumbs and
  typed links make recovery cheap (web agents backtrack often —
  WebRollback `pa530004`).
- **Focused context beats full reads** for sparse evidence: LongMemEval
  87.0% evidence-only vs 60.6% full history (`pa529980`); NoLiMa shows
  GPT-4o at 69.7% of baseline by 32K (`pa529978`); distractor
  sensitivity (`pa529977`) backs the residency argument. Exception:
  Self-Route found full context wins when evidence is dispersed
  (`pa529982`) — which is why `more` survives for straight-through
  ingested reads and over-budget refuses with an outline instead of
  truncating.
- **A cheap categorizer is enough for pack selection** — Adaptive-RAG
  routed none/single/multi-step at near-parity accuracy in ~1 step
  (`pa529990`); RAG-MCP retrieved the relevant MCP schema at ~half the
  tokens of exposing all, but at 43% selection accuracy (`pa529993`),
  so misroute rate needs measuring and search stays the fallback.
- **Tool and doc text wants a lint, not more length**: 97.1% of 856
  MCP tool descriptions carry a defect, and augmentation raised
  success 5.85 points while costing +67% steps (`pa529998`). Gate doc
  changes on tokens per correct answer, not correctness alone.
- **The optimizer pattern is proven**: DSPy/GEPA-style compile and
  select beat hand prompts under held-out evaluation (`pa529995`,
  `pa529996`); DRAFT rewrites docs from execution feedback
  (`pa529997`). Keep a change only when held-out correctness holds.

Contradictions to fold in, not around:

- **8b is unproven for tool-heavy runs.** Masking or stubbing old tool
  results matched or beat summaries at lower cost (SWE-agent
  `pa521204`; Complexity Trap `pa529984`; systematic study `pa529989`,
  free-form summary −11 points). Context-folding matched a 10× larger
  ReAct history (`pa529986`); pruning plus a state summary won one
  enterprise benchmark (`pa529988`). Rewriting the prefix also breaks
  the cache (ACON `pa529985`; Don't Break the Cache `pa529987`), so
  the rolling summary must live after the stable prefix. Treat the
  asa pattern as a hypothesis slice 2's telemetry tests.
- **Preloaded packs can cost more than they save.** Generated context
  files scored −0.5 to −2 points at +20% cost (`pa529999`); compact
  docs did not help when the agent could read the source (`pa530000`).
  Packs stay A/B-tested (slice 8), and the harness question bank
  should come from doc-dependent tasks.
- **Very small blocks are not free.** LongRAG's 4–6K retrieval units
  raised recall (`pa529974`); dispersed-evidence tasks prefer full
  context (`pa529982`). The 500–2000 target fits an agent reader on
  designed content, not a universal optimum — the lint should watch
  for over-splitting.
- `llms.txt` has no controlled evaluation — do not cite it.

## Slices (dependency order)

1. **Authoring lint** (S). Extend `test_skill_size.py` into a block lint:
   block size, heading structure, link validity. Report-only first; then
   drop `FAIL_BYTES` below the 24 KiB split point so no skill can need
   `more`.
2. **Telemetry** (S). `resp_bytes` and per-session roll-up on
   `tool_calls`: hops, bytes and `more` calls per session. The before
   number, and the acceptance test's meter.
3. **Block metadata** (S–M). Heading or frontmatter conventions for
   parent, summary and typed links; `~N` and `/toc` reads show the
   breadcrumb and link lines. Flat files keep working.
4. **`budget=` + refuse-with-outline** (M). Token estimate; over budget,
   return the outline (skill `/toc`, md outline, paper toc) instead of
   `_pagination.split`. Skills and md first.
5. **Split the 18 skills over 24 KiB** (M–L, content). The five at 32 KiB
   first; then remove `more` for designed kinds.
6. **Batched `get(id=[...])`** (S–M). Extend the existing gripe batch
   read; dispatcher fan-out under one budget, skill, md and paper first.
7. **Search over block summaries** (M). Summaries indexed as their own
   chunks; hits carry `slug~id` plus breadcrumb at any depth.
8. **Starter packs** (M). n packs per problem type (indexes + most-used
   blocks), chosen by job type or a prompt categorizer, delivered as a
   session-start prefix; reuse the pinned-skills banner and `have=`. A/B
   packs against the harness (slice 9).
8b. **Summary-seeded turns** (M). The asa pattern for MCP agents: each
   task or turn starts a fresh agent with prefix + rolling summary;
   tool-result stubs inside a run. Measure summary cost against carry
   saved (slice 2).
9. **Acceptance harness** (M). Per-area question bank; a small-model agent
   starts with only the TOCs; score answer correctness, tokens, hops,
   wrong branches (block fetched then abandoned). Builds on slice 2.
10. **Optimizer** (M). Reads harness scores and telemetry; proposes
    splits, TOC-line rewrites, `requires` links where agents backtrack, and
    preload promotions; re-runs the harness and keeps a change only if
    correctness holds and tokens drop. Proposes; a builder lands.

Later: RAPTOR-style summary trees for papers (ingest-side).

## Acceptance

- No designed-content read ever returns a `more` footer.
- An over-budget `get` returns an outline with addressable ids.
- The harness shows lower tokens per correct answer than the pre-slice
  baseline, at equal or better correctness.

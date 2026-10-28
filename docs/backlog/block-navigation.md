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
- Batched get: the `get` signature accepts a list; no kind implements it.
- Telemetry: the `tool_calls` ledger (`tool_ledger.record_call`) logs
  verb, kind, outcome, latency; no response bytes, no per-session
  roll-up, `result_count` unpopulated.

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
6. **Batched `get(id=[...])`** (S–M). Dispatcher fan-out under one budget;
   skill, md and paper first.
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

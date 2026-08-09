---
status: draft
title: Taproot [pa]-arm — migrate whole-paper draft cites through taproot, retire the [pa] token
model: opus
blocked-by: taproot-draft-citation-view
---

# Taproot `[pa]`-arm — whole-paper cites → claim cites

## Status — slices 1 + 2 built

**Slice 1** (stub-skip + ref-level promote, `taproot/backfill.py` +
`cli/taproot.py`): the segmenter anchors on bare `[pa<id>]` groups (kept
separate from `[pc]` groups — a kind switch breaks contiguity), and each
`[pa]` group is routed by the cited paper's block-count:

- **stub `[pa]`** (0 blocks) → `stub-fetch-first`, **skipped**, no write (AC2);
- **fetched `[pa]`**, `--ref-level` override → promotes **ref-level**
  (ungrounded) and rewrites `[pa]`→`[fi<hub>]` (AC3), inheriting `apply_chunk`'s
  idempotent re-convergence retirement invariant (AC4). `plan_chunk` dry-run
  reports the per-group action and writes nothing (AC5).

**Slice 2** (the `[pa]`→`[pc]` **re-ground** action — AC1, resolves open #3):
a fetched `[pa]`'s **default** action (no `--ref-level`) is now a re-ground,
not the slice-1 `reground-needed` no-op. Contract:

- **Grounding-chunk suggestion (open #3, settled — reuse chase's locate).**
  Read the cited paper's body chunks `(chunk_id, ord, text)`; a deterministic
  lexical pick (unigram overlap with the span, mirroring
  `chase._select_target_chunk`) proposes the best passage, then
  `_chase_llm._locate_chunk_in_target` (Tier.MEDIUM) confirms-or-corrects it.
  The locate is injected (`locate_fn`), so tests run with a deterministic fake
  and no LLM/embedder — matching the cascade fns. It fires only on the write /
  dry-run path, never a view read.
- **Re-ground is a cite *refinement*, not a promote — NO hub minted.** It
  rewrites the prose token `[pa<id>]`→`[pc<chunk_id>]`. The result then rides
  the **existing** `[pc]` promote path unchanged (two-step: re-ground, then a
  subsequent backfill run mints the chunk-grounded hub — AC1). Refining
  paper→passage never loses information (the passage is *in* the paper), so it
  needs no hub-then-prose retirement window.
- **All-or-nothing per contiguous run.** A multi-supporter run (`[pa1][pa2]`,
  one span, both fetched) re-grounds each supporter to its own best chunk →
  `[pc<c1>][pc<c2>]` (which the `[pc]` path then folds to one hub). If locate
  returns **None** for *any* supporter in the run, the whole run's rewrite is
  skipped (`reground-nomatch`, no write, prose left `[pa]`) — the same
  token-erasure guard as the slice-1 mixed-run rule (append-only chunks).
- **Actions:** `reground` (chosen targets, rewrites on apply) /
  `reground-nomatch` (locate found no passage — no write; author re-grounds by
  hand or re-runs with `--ref-level`).

## Motivation / why

`taproot/backfill.py` migrates a draft's `[pc<id>]` (paper-**chunk**) cites onto
claim hubs and rewrites the prose token `[pc]`→`[fi<hub>]`. It is **`[pc]`-only**
(`_PC_HANDLE_RE`). But drafts also carry `[pa<id>]` (whole-**paper**) cites —
notably to un-fetched **stubs** (0 blocks, no chunk to ground an evidence edge).
The user's ask names this exactly: *"go from a paper `pa` cite to a cite that
goes through taproot, then the `pa<id>` should go away afterwards."* This
proposal defines and builds that arm.

It depends on the citations view (shipped; `handlers/_citations_view.py`)
(the read-only lifecycle view): that proposal already displays `[pa]` rows in
**to-fetch** / **to-re-ground**; this one gives those rows a working action.

## The `[pa]` arm (two entry states)

A `[pa<id>]` cite is one of:

- **stub `[pa]`** (paper block-count 0) — **not promotable**: an evidence edge
  would be ungroundable (no chunk). It sits in the view's **to-fetch** partition
  until the paper ingests. On ingest it becomes a fetched `[pa]` (below). *This
  proposal does not promote stubs; it routes them via fetch first.*
- **fetched `[pa]`** (block-count > 0) — **re-ground first, then promote.** The
  paper has chunks, so the honest grounding is a specific passage. Offer a
  `[pa]`→`[pc]` re-ground (pin the chunk that supports the sentence); the
  re-grounded `[pc]` then rides the **existing** `[pc]` promote path unchanged.

**Decision (readiness open #2): re-ground is the default; ref-level promote is
an explicit override.** A fetched `[pa]` promoted directly would mint a
**ref-level (ungrounded)** evidence edge — `seed_claim_hub` already flags this
via its `ungrounded` counter. That loses passage grounding, so the default
action on a fetched `[pa]` is the `[pa]`→`[pc]` re-ground; only on an explicit
author override does it promote ref-level (whole-paper claims that genuinely
have no single grounding passage — e.g. "X is a landmark result").

## The retirement invariant (readiness blocker #6 — stated to match reality)

`backfill.py::apply_chunk` is **not atomic** and this proposal does not pretend
it is. Its real, sufficient guarantee is **idempotent re-convergence**: the hub
mint + evidence edge commits in its own transaction *first*; the prose token
rewrite (`[pc]`/`[pa]`→`[fi]`) is applied via the draft edit door *after*. A
failure between the two leaves the direct token **in place and still grounded**
(the paper is still cited); a re-run converges onto the same hub (content-derived
`pub_id`) and completes the rewrite. So the invariant is:

> A direct `[pa]`/`[pc]` token is retired **only after** its hub evidence edge is
> committed. The window between is safe because the un-retired token is still a
> valid grounded cite, and re-running the promote is a no-op-or-complete.

The `[pa]` arm reuses this same door and inherits this guarantee. It does **not**
require, and does not add, cross-write atomicity infra. (This corrects the
original combined proposal's "atomic" wording, which misdescribed the primitive.)

## In scope

- Extend `backfill.py`'s cite-group segmenter to recognize `[pa<id>]` groups
  alongside `[pc<id>]` (add a `pa` handle pattern beside `_PC_HANDLE_RE`),
  feeding the **same** cascade (`extract_claim → block → dedup_judge → place →
  apply_placement`).
- **Stub `[pa]` → skip with a "fetch first" reason** (surfaced in `plan_chunk`
  dry-run); never mint an ungrounded edge for a stub.
- **Fetched `[pa]` → re-ground action** (`[pa]`→`[pc]` chunk pin) as the
  default; an explicit `--ref-level` override mints the whole-paper edge and
  rewrites to `[fi]`, accepting `seed_claim_hub`'s `ungrounded` flag.
- Wire the view's **to-re-ground** row action to the re-ground, and its
  **to-promote** row (post-re-ground) to the existing promote.

## Explicitly NOT in scope

- **The `[pc]` promote path** — unchanged; the re-grounded `[pc]` uses it as-is.
- **The read view** — built by the predecessor proposal.
- **Cross-write atomicity infra** — explicitly not needed (see the invariant).
- **Corpus-wide sweep** — still deferred to the hub-refine reconcile worker
  (`taproot-hub-refine.md`); this stays per-draft, dry-run-first.
- **Selectivity implementation** — reuses the cascade's existing
  `extract_claim` NO-CLAIM gate; no second detector.

## Acceptance criteria

1. A fetched `[pa<id>]` cite offered through the arm produces a `[pa]`→`[pc]`
   re-ground suggestion (a specific chunk pin); accepting it rewrites the draft
   token `[pa]`→`[pc]` and the result is then promotable by the **existing**
   `[pc]` path, yielding a **chunk-grounded** (not `ungrounded`) evidence edge.
2. A **stub** `[pa]` cite run through the arm is **skipped** with a "fetch first"
   reason and mints **no** edge and **no** hub (asserted: `links` unchanged).
3. Promoting a fetched `[pa]` with explicit `--ref-level` mints a ref-level
   edge, rewrites `[pa]`→`[fi<hub>]`, and the run reports `ungrounded=1`.
4. **Retirement invariant** (idempotent re-convergence): if the prose-rewrite
   step is forced to fail after the edge commits, the draft still shows the
   direct `[pa]`/`[pc]` token (grounded), and a re-run completes the rewrite
   with no duplicate hub or edge — asserted by a fault-injection test.
5. `plan_chunk` dry-run over a chunk mixing `[pa]`(stub), `[pa]`(fetched), and
   `[pc]` cites reports the correct per-group action (fetch-first / re-ground /
   promote) and writes nothing.

## Target + blast radius

- **`taproot/backfill.py`** — segmenter extension (`pa` pattern), per-group
  action routing (stub-skip / re-ground / ref-level promote), reusing
  `apply_chunk`'s existing hub-then-prose ordering.
- **`taproot/authoring.py`** / `hub.py` — reused unchanged (ref-level vs
  chunk-grounded already supported via `source_handle` presence /
  `ungrounded`).
- **`cli/taproot.py`** — surface the re-ground suggestion + `--ref-level` flag.
- **Skills**: `precis-finding-help` / taproot skills note the `[pa]` arm.

## Open questions / decisions log

1. **[RESOLVED — re-ground default, ref-level on override]** See the arm section.
2. **[RESOLVED — idempotent re-convergence, not atomicity]** Retirement
   invariant restated to match `apply_chunk` (readiness blocker #6).
4. **[known limitation — slice 1] Stub detection uses `store.count_blocks`,
   which is not `retired_at`-aware.** A fetched-vs-stub `[pa]` is classified by
   `count_blocks(ref_id) > 0` (matching `_citations_view._partition_of`, so the
   drive/view/arm agree). `count_blocks` counts `ord >= 0` rows without a
   `retired_at IS NULL` filter, so a paper whose only body chunks were
   soft-retired (re-ingest/dedup) classifies as "fetched" though it has no live
   passage — it would route to `reground-needed`/ref-level rather than
   `stub-fetch-first`. Low-likelihood, pre-existing across all taproot stub
   detection; making `count_blocks` retirement-aware is a shared-primitive
   design call for the main loop, deferred rather than forked here.
3. **[RESOLVED — reuse chase's locate, injected].** The `[pa]`→`[pc]`
   re-ground suggests the grounding chunk by reusing the
   `chase._select_target_chunk` pattern: a deterministic lexical pick over the
   cited paper's chunks, confirmed/corrected by `_locate_chunk_in_target`
   (Tier.MEDIUM). Injected as `locate_fn` (deterministic fake in tests). Fires
   only on the write / dry-run path, never a view read. See slice-2 status.

## Cross-references

- the citations view (shipped; `handlers/_citations_view.py`) —
  predecessor (the view whose `[pa]` rows this activates).
- `taproot/backfill.py`, `taproot/authoring.py`, `taproot/hub.py` — the engine
  extended/reused.
- `docs/decisions/0073-...` / `0074-...` — write door + render, unchanged.
- `docs/backlog/taproot.md` — #15 (only paper-grounded claims become hubs) is
  respected: a fetched `[pa]` is paper-grounded by construction.

---
status: draft
title: Nightly fixer for drifted cites — re-sync the prose, don't just flag it
prio: normal
model: opus
pillar: memory-graph
---

# Nightly fixer for drifted cites

The detection half shipped 2026-09-18 (`cite-pins-hub-version`, now
deleted): a draft cite pins the hub `pub_id` **and title** it was written
against, drift shows in `view='hygiene'` quoting both statements, and a
drifted cite blocks export. Unsigned hubs are a counted advisory;
pre-pin cites read as unknown, never drift.

That is all **pull**. Nothing re-syncs the prose — a drifted cite waits
for a human or an agent to notice it, or for an export to refuse.

## Motivation / why

Reto, 2026-09-18: "I like your approach. We can still fix nightly or
something." Decided then: ship the pull surfaces first and add the fixer
only once the hygiene counts show the volume justifies the spend.

Deliberately **not** an event-consuming worker hanging off the reword
door. Re-syncing a paragraph is an LLM rewrite, not a substitution, so
firing per-reword means minting N jobs inside a synchronous write path —
the shape this repo refuses when it keeps embeddings in the worker rather
than in ingest. A nightly batch also coalesces for free: it sees all
drift at once instead of one reword at a time.

## In scope

- A scheduled pass over `_draft_lint.find_drifted_cites` for each
  opted-in draft, minting a fix job per (draft, section) — not per chunk,
  or a 40-hub reword sweep mints hundreds.
- **Pass the `fi<id>` in the job params** (Reto's note) so the worker
  reads the old and current statements off the hub and its alias chain
  rather than rediscovering what moved. The edge already carries
  `cited_title`, so the "was/now" pair is available without a reverse
  lookup.
- The fix itself follows [[precis-claim-fidelity-help]]: re-sync the
  sentence to what the hub now says, at a strength its trust state
  carries — not a paste of the hub sentence.

## Explicitly NOT in scope

- Any change to detection. The three surfaces shipped and work.
- Auto-applying without the review ledger. A fix re-derives the chunk's
  `content_sha`, which re-opens it for human sign-off; that stays.
- Drafts with `authoring` off — the fixer files the change request, it
  does not write prose there.

## Acceptance criteria

- A drift left by a reword is picked up on the next nightly tick and
  either re-synced (authoring on) or filed as an anchored change request
  carrying both statements (authoring off).
- One reword touching many cites yields one job per (draft, section).
- A draft with no drift mints nothing.

## Decisions (Reto 2026-10-21)

- Measure first: one read-only pass of `view='hygiene'` drifted-cite counts
  across drafts with hub cites. Build the fixer only if the total is ≥20
  drifted cites; below that, record the count here and stop.
- If built: gate on the existing `meta.draft_refresh.enabled` opt-in (one
  switch per draft), and a blocked export does not kick the fixer.

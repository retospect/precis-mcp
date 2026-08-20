---
status: draft
title: 297 claim hubs have a refs.title that differs from their ord=0 body chunk — search and dedup run on the wrong string
model: sonnet
---

# One hub in five says two different things

Measured 2026-08-19 over the live claim cohort (`kind='finding'` +
`TAPROOT:claim`, 1,524 hubs):

| | hubs |
|---|---|
| `btrim(title) = btrim(body)` | 1,227 |
| **diverged** | **297 (19.5%)** |
| no `ord=0` chunk at all | 0 |

Found while picking an exemplar hub, not by any check — nothing in the
codebase compares the two.

## Why this is worse than a cosmetic split

**Embeddings are computed from the chunk, not the title.** So for 297 hubs:

- semantic search and `view='toc'` match against text no human reads;
- the ANN blocking step in `taproot/canon.py::block` — the first stage of the
  dedup cascade — compares the *chunk*, so two hubs whose titles are near
  identical can fail to block if their chunks diverged, and vice versa;
- the corpus lint triage in `nanopub-corpus-remediation.md` ran over
  `refs.title`, so for one hub in five its verdict describes a different
  string than the one search sees.

`pub_id` is derived from the sentence at mint time, so a diverged hub's
identity may correspond to neither current string.

## Two incidents, not one — and they need opposite repairs

Grouping the 297 by `refs.created_at`:

- **~209 hubs, 2026-06-15 → 2026-07-04** — body is *longer* than title in
  every case (0 title-longer). Pre-dates `refine_claim_sentence`; the title
  was a short label and the chunk carried the real sentence.
- **~42 hubs, 2026-08-03/04 and 2026-08-17** — title is *longer* (34 of 38 on
  08-04 alone). This is the taproot regrounding pass enriching `refs.title`
  without syncing the chunk.

So the authoritative side differs by cohort: **title** for the August set,
**body** for the June set. A single blanket rule would destroy content in one
group or the other — the same trap as the 200-char repair, where restoring
frozen hubs from the chunk would have reverted 21 deliberate reviewer
rewordings (root-caused and repaired 2026-08-17, 9d0b9206; the two hardening
residuals live in `mcp-staleness-title-roundtrip-guards.md`).

Example: fi191126 — title 224 chars, body 115, both coherent sentences, the
title strictly richer.

## Root cause is the same shape as the truncation bug

A writer that updates `refs.title` without going through
`taproot/hub.py::refine_claim_sentence`, which is the only door that updates
all three sites (title, `ord=0` chunk, `pub_id`) in one transaction. The
generic `store/_refs_ops.py::replace_ref_text` is not the culprit — it is
reachable only from `handlers/quest.py` and `handlers/todo.py`. The
regrounding pass is the likely August writer; identify it before repairing,
or the repair re-diverges on the next run.

## The body is internal — it is never published

Traced 2026-08-19. `nanopub/evidence.py::HubBundle.sentence` is
`hub_ref.title`; the artifact, AIDA URI, `claim_sha` and drift all read the
title. `assemble.py` never touches the body. `bundle.body` is referenced in
**exactly one place in the codebase**: `nanopub/gates.py` (~line 216), the
acquisition-marker hearsay gate.

So the `ord=0` chunk has two jobs, both internal: embedder input, and that one
gate. Nothing about it is externally visible. This is what makes the
"one authored sentence, reassembly on export" design safe — see
`nanopub-corpus-remediation.md`.

### Precondition: migrate the 6 acquisition markers first

The hearsay gate reads `body` **because** it currently differs from `title` —
its own comment says "title alone misses the harvester's 'not in corpus'
note". Measured over the live cohort:

| | hubs |
|---|---|
| `ACQUISITION_MARKER` matches **body** | 6 |
| matches **title** | **0** |
| body-only | **6** |

Deriving the chunk from the title would silently disable that gate for all
six. The gate blocks a hub from being grounded in a *citing* paper when its
own prose admits the primary source was never ingested — a real
provenance check, not a nicety.

The fix is not to keep the divergence but to stop encoding a machine-readable
fact as prose: move "primary source not in corpus" to a tag (or the existing
hanging-mint path) so the gate reads it structurally. That is strictly better
than today — `ACQUISITION_MARKER` is a regex over free text
(`not (yet )?in (the )?corpus|needs? acquisition`), so a reworded note like
"primary not yet acquired" already slips past it.

**Do not derive the body chunk until those 6 are migrated.**

## Work

1. **Find and fix the writer.** Until then any repair is temporary.
2. **Repair per cohort**, not globally: August → chunk from title, June →
   title from body. Re-derive `pub_id` after, and re-run the duplicate scan,
   since collapsing divergence can surface hidden duplicates (this already
   happened twice: fi191259/191268 and fi191179/191260).
3. **Detection has landed** — `title-body-divergence` and
   `missing-body-chunk` in the `precis taproot lint` cohort sweep, reporting
   only. Do not add either to `--fix`; see the opposite-repair argument above.
4. Sequence this **before Phase 3.1 (notation normalization), not just before
   re-approval.** `refine_claim_sentence` — the door every repair pass writes
   through — sets the title *and derives the body chunk from it*. So
   normalizing a June-cohort hub would overwrite its good body text with the
   normalized short label. The ~209 June hubs must have the better text
   promoted into `title` first; only then is deriving the chunk safe.
   Re-approval comes after both, per the drift-ordering constraint in
   `nanopub-corpus-remediation.md`: repairing a title once its `claim_sha` has
   frozen re-triggers gate #14.

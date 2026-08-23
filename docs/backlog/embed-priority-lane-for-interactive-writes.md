---
status: draft
title: EmbedHandler needs a draft/conv priority lane so an interactively-edited chunk isn't invisible behind the ~1M-chunk backlog
prio: normal
---

# EmbedHandler needs a draft/conv priority lane

From gripe 244419's fix plan (item B). Independently justified — this is a
real defect whether or not the MCP wedge is fixed.

## Motivation / why

Chunk writes correctly leave the embedding to the worker (see the correction
in gr244419: `DraftStore.edit_text` UPDATEs `text` + `content_sha` and the
worker re-derives on the sha mismatch — nothing embeds inline). But
`workers/embed.py::EmbedHandler` declares only `skip_chunk_kinds` and no
queue tier of its own; its ordering comes from the shared worker base. With
the ~1M-chunk paper backlog `workers/chunk_keywords.py` documents, a chunk
the user just renamed in a draft is semantically invisible not merely for a
while but *indefinitely* — it sits behind the whole corpus.

The precedent already exists twice over: `workers/chunk_keywords.py` carries
an explicit conv/draft priority rationale, and
`workers/llm_summarize.py::_FRESH_TIERS` implements the full
`draft > conv > hot > rest` reader-salience ordering with each tier's
`(kind_pred, extra_pred, order_by)` spliced into `_FRESH_CLAIM_SQL`. Embed is
the odd one out.

## In scope

- Give `EmbedHandler` an explicit fresh-claim tier ordering modelled on
  `_FRESH_TIERS` (draft/conv ahead of the rest).
- The small tail of genuinely-inline write-path embeds:
  `handlers/_cache_base.py`'s block insert (already catches
  `EmbedderUnavailable` and stores `embedding=None`, so this is mostly
  making the fast path match the fallback) and `handlers/orcid.py`'s
  `embed_one(card)`.

## Explicitly NOT in scope

- The interactive/batch timeout split (gr244419 item A) — ships separately
  and first.
- The scope-aware embed-status hint — see
  `embed-status-hint-three-state.md`.
- Any cap on how many chunks one edit may touch. Explicitly rejected by the
  user: a legitimate rename-all touches many chunks and refusing just forces
  hand-sharding. Report the fan-out (`{touched: N, embed_stale: N}`), don't
  limit it.

## Acceptance criteria

- A draft chunk edited now is claimed by the next embed pass ahead of
  backlog paper chunks, demonstrably (a test that seeds both populations and
  asserts claim order).
- Corpus-wide embed throughput is unchanged when no priority-tier chunks are
  pending — the tiers partition, they don't add a scan.
- `_substitute`'s reported result includes the fan-out counts.

## Target + blast radius

`workers/embed.py` (claim query), `handlers/_cache_base.py`,
`handlers/orcid.py`, `handlers/draft.py::DraftHandler._substitute` (result
body). Worker queue ordering — watch the embed backlog burn-down rate
post-deploy.

## Open questions / decisions log

- Does the shared worker base already expose a tier hook `llm_summarize`
  uses, or does `llm_summarize` implement `_FRESH_CLAIM_SQL` privately? If
  the latter, decide whether to lift the tier machinery into the base or
  copy it. Read `workers/llm_summarize.py` around `_FRESH_TIERS` first.

---
status: ready
title: batch put(kind='draft', chunk_kind='term', terms=[...]) — the 500-round-trip glossary case
pillar: platform
prio: medium
---

# Batch `put(draft, terms=[...])` — the remainder of the batch-form item

`get(id=[...])` and `tag(id=[...])` on gripe, alert and todo shipped
(`NumericRefHandler._coerce_ids` / `_get_batch` / `_tag_batch`, opt-in per
kind via `batch_ids`; `Store.atomic()` makes the tag batch one transaction).
What is left is the third measured loop.

## Measured — surface-review pass #1, 2026-09-29

| (verb, kind) | calls | runs | note |
|---|---|---|---|
| `put, draft` | 141 | 4 | the term-glossary case: a draft with 508 undefined abbreviations, one `put(chunk_kind='term')` each |

## In scope

A batch `put(kind='draft', chunk_kind='term', terms=[…])`: one call creates
many term chunks on a draft. Transactional (all or nothing) with per-term
outcome lines, same shape as the tag batch.

## Explicitly NOT in scope

- Changing `search`'s ranked-result contract.
- Batch `delete` — destructive, wants its own decision.

## Acceptance criteria

- A 500-term glossary lands in a handful of calls, not 500.
- A rejected term rolls back the whole call and names the term.

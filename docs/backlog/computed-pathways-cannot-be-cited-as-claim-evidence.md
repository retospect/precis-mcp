---
status: draft
title: a claim hub cannot cite a computed pathway as evidence
prio: normal
---

# a claim hub cannot cite a computed pathway as evidence

## Motivation / why

Two identity schemes coexist and never meet:

- **pathway** — content-addressed. `pslug = f"{candidate.slug}-rx-{key[:10]}"`
  (`quest/compute.py::dispatch_autocatpath`), where `key` is
  `_autocatpath_content_key(config, slab_extxyz)`: a sha over the engine token,
  the reaction config and the exported slab geometry. Identity means
  *reproducibility* — same inputs, same slug, dedup onto the same job.
- **citation ladder** — provenance-addressed. `[pa]` paper → `[pc]` paper chunk
  → `[fi]` claim hub. Identity means *where an assertion came from*.

A pathway reaches its candidate (`meta.candidate_ref`, plus the slug prefix)
and thence its quest (the candidate's `serves` link). That much works. But
measured on prod (2026-08-24), every link touching a `pathway` ref is
`related-to` — 7154 outbound, 237 inbound, and **zero** `evidence` /
`supports` / `corroborates`.

So an `[fi]` hub can cite a paper chunk and cannot cite our own compute. The
claim this quest exists to produce — "DFT gives a barrier of X eV on this
facet" — has no edge type that expresses its actual support, which means such a
claim is either unsupported in the graph or leans on a paper that says
something adjacent.

## The content key is an ASSET here, not an obstacle

A pathway citation should pin `(pathway_ref_id, content_key)`, not the ref
alone. A changed engine version, geometry or reaction config produces a
DIFFERENT content key, mints a new pathway, and stamps the prior
`meta.status = "superseded"` / `superseded_by` (gr197692). A claim pinned to
the key therefore *breaks visibly* when its evidence is invalidated — a
property the paper-chunk side does not have, where a re-ingested paper can
shift chunk boundaries under a live citation.

## In scope

- An evidence relation from `[fi]` → `pathway`, parallel to `[fi]` → `[pc]`.
- Pin `(pathway_ref_id, content_key)` at citation time; surface a citation
  whose pathway is now `superseded` as stale rather than silently wrong.
- A compute analogue of the verbatim-quote check. There is no text to quote,
  so `_validate_quote`'s substring+uniqueness test does not transfer; the
  equivalent is `(measure key, value, uncertainty)` read back out of the
  pathway's `results` (see `precis_pathway/persist.py::pathway_meta`) and
  re-checked in code against the claim's stated magnitude.

## Explicitly NOT in scope

- Changing the pathway content-key scheme. It is doing its job; this item
  consumes it.
- Minting/signing/publishing semantics — a compute-backed nanopub still goes
  through the same human gate as any other.
- Retrofitting the 237 existing `related-to` edges. New citations first;
  decide on backfill once the relation exists.

## Acceptance criteria

- An `[fi]` hub can carry a pathway as evidence, and the graph distinguishes
  that from a `related-to`.
- A citation whose pathway was superseded by a re-dispatch is reported as
  stale, naming the superseding pathway.
- A claimed magnitude that does not match the pathway's own `results` fails
  validation rather than being accepted on the LLM's word.

## Target + blast radius

`quest/compute.py` (citation mint at dispatch/persist time),
`precis_pathway/persist.py`, the finding/claim link schema, and the nanopub
admissibility rubric (a compute-backed claim needs its own admissibility
shape). Touches the claim graph — coordinate with td244453's campaign before
landing.

## Open questions / decisions log

- Does a compute-backed claim need a distinct hub type, or is `[fi]` with a
  pathway evidence edge enough? Leaning the latter — the hub asserts, the edges
  explain what supports it, and mixing paper + compute support on one hub is
  desirable rather than a problem.
- Should the pathway's `config_snapshot_yaml` be part of what a citation pins,
  or is the content key (which already folds it) sufficient? Key is probably
  enough; the snapshot is for humans reading the audit trail.

---
status: in-progress
pillar: memory-graph
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
  desirable rather than a problem. **Settled by the build:** `[fi]` + the
  `corroborates`/`tier: "computed"` edge is what shipped; no new hub type.
- Should the pathway's `config_snapshot_yaml` be part of what a citation pins,
  or is the content key (which already folds it) sufficient? Key is probably
  enough; the snapshot is for humans reading the audit trail.

## Status 2026-09-17

Slice 1 (evidence edge + tier + content-key pin) implemented in
`src/precis/taproot/hub.py`: `attach_evidence` now accepts a `pathway` src
(new `PATHWAY_EVIDENCE_KINDS`, deliberately separate from
`EVIDENCE_SRC_KINDS` — the latter still feeds `taproot/seniority.py`'s
citation-graph originator derivation, meaningless for compute) and writes
the edge with the existing `corroborates` role only (never `establishes` —
enforced), reusing `store.add_link`, no new relation, no migration. Edge
`meta` gains `{"tier": "computed", "content_key": ..., "pathway_ref_id":
...}`. Guards (`_pathway_evidence_meta`, reading `meta['results']
['trust_summary']['barrier']['available']` per
`precis_pathway/persist.py::pathway_meta`): refuses a `meta.status ==
"superseded"` pathway naming `superseded_by`; refuses when
`trust_summary.barrier.available` isn't truthy, naming the field; refuses a
pathway with no `meta.content_key` to pin. Chunk-grounding and the
Crossref retraction check are both skipped for a pathway src (no text, no
DOI) — the existing `check_retraction and kind == "paper"` gate already
excludes it.

## Status 2026-10-02 — read path

Slice 2: `taproot/seniority.py` reads pathway edges
(`_READ_SRC_KINDS`) and routes them to a separate
`HubEvidence.computed: list[ComputedEdge]` — outside the originator walk,
the coverage note, `sole_derivative` and the print set (a pathway has no
cite_key). Kept out of `corroborators` on purpose: every `EvidenceEdge`
consumer formats `paper_ref_id` as a `pa` handle and looks up cite_keys.
The claim page (`claim/_body.html.j2` "Computed evidence"), the popover
count and the fisheye Claims block (`refeye._computed_line`) render it.
Each `ComputedEdge.stale_reason` is set at read time when the pathway is
`superseded` (naming `superseded_by`) or its current `meta.content_key`
differs from the edge's pin.

`taproot/repair_evidence.py`'s `paper`/`patent` filter is correct as is:
that pass re-grounds an edge against source *text*, which a pathway has
none of.

A hub whose only evidence is computed stays `inflight` / `unverified` in
`claim_trust` — nothing prints, so the label is accurate; a computed-tier
trust label is a decision for the admissibility work below, not a read fix.

## Status 2026-10-13 — the four open parts landed

- **Fresher re-dispatch of a `ready` pathway** (was Still-open 1):
  `dispatch_autocatpath` stamps `meta.reaction_key` (sha of the reaction
  part of the content-key inputs — canonical spec, no engine token, no
  slab) at mint, and supersedes a prior `ready` pathway in the same tx
  when the new mint's `reaction_key` matches — exactly the `computing`
  case's stamp. A `ready` prior for a different reaction (or one
  predating the stamp) is untouched. The read-time `stale_reason` now
  sees the supersession.
- **Magnitude re-check** (was Still-open 2): `hub.py`'s
  `pathway_barrier_mismatch` is the `_validate_quote` analogue, shared
  by both doors. A caller passing `meta.claimed_barrier` (±
  `claimed_barrier_std`) gets it compared against the pathway's
  `meta.graph.links` reaction-step `barrier`/`barrier_std` — a mismatch
  beyond the stated uncertainty (or `2*barrier_std` with none) refuses
  with a `BadInput` naming both numbers; `ComputedEdge.mismatch_reason`
  re-computes the same note at read time and the claim page + fisheye
  render it. No LLM call.
- **Nanopub bundle visibility** (was Still-open 3): `_EVIDENCE_KINDS`
  admits `pathway`; `HubBundle.computed` carries the edges as a separate
  list (never folded into the DOI-anchored `sources`), rendered as
  `pw<id>` nodes in the claim page's provenance DAG.
- **Web attach form** (was Still-open 4): the `/nanopub/fi<id>/evidence/add`
  door parses `pw<id>` beside `pa`/`pt` and surfaces the refusal text.

## Status 2026-10-15 — admissibility rubric, blocking half

`nanopub/gates.py::check_computed` (run unconditionally in
`run_mint_gates`, so at approve AND at sign — a pathway superseded
between the two blocks the sign; `frozen` has no arm because the pin IS
the comparison, there is no frozen copy of compute results). Three
violations per edge: `computed-pin` (edge carries no `content_key` —
unverifiable citation, written around the attach door), `computed-stale`
(`ComputedEdge.stale_reason` — superseded names the successor; key drift
names both keys), `computed-mismatch` (`ComputedEdge.mismatch_reason` —
the edge's stated barrier re-checked against the pathway's CURRENT
`graph.links`). Plus: a `hypothesis` mint on a hub carrying computed
edges refuses (`schema-lint`, mirroring the passages arm — motivation,
not evidence).

Deliberately unchanged, pending the calls below: a computed edge does
NOT satisfy the claim schema gate's grounding requirement (a
computed-only claim still mints only `hanging`), `approve`'s human gate
is untouched, and the artifact serializes nothing about computed edges —
a minted claim backed by a pathway still publishes with no provenance
node for it.

Still open — design calls for Reto:

- Does a live computed edge satisfy the claim's grounding requirement
  (a computed-only claim mints non-hanging), or must a compute-backed
  claim still carry a grounding passage / mint hanging?
- What does the artifact assert for computed evidence — the pathway as
  a provenance source node anchored by its content key (e.g.
  `urn:precis:pathway:<key>` + `precis:evidenceTier "computed"` + the
  stated barrier/std literals), or a PROV activity/agent shape?
- Should `approve` freeze the computed pin set into the payload
  (`_freeze_source_anchor`-style) so sign checks the frozen set, or is
  the live-edge re-check the right authority?

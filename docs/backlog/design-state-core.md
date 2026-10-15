---
status: in-progress
pillar: 3d-design
title: design core — shared scenarios, provenance, design history, and discrete-states machinery for se
prio: high
model: opus
---

# Design core — shared scenarios, provenance, design history, discrete states

The shared substrate the multiscale spec's subsystems stand on
(multiscale-design-system-spec.md §1.3–1.6, §5.6): one internal package,
`src/precis/design/`, rented by `se` (nm merged into se's atomic mode), no
kind or verb of its own. Every in-scope item has shipped on a branch; what
is left is the deploy and its check. `git log -- src/precis/design
src/precis_se` has the history; the surfaces are in skill
`precis-se-help` §Design history and provenance and in the
`precis.design` package docstring.

Shipped (rounds 1–3 2026-09-14; the rest 2026-10-15): scenarios and
presets; block uids carried across saves, every in-design cross-reference
keyed by uid, cross-design `template_ref` keyed by (slug, uid) (se
migration `0020`); discrete states + transitions; envelope revisions;
named `checkpoint`/`restore`, `pin` → branch with BranchResult,
`view='branches'`, `view='diff'`; per-number provenance on block facets
and measures with the margin audit (se `0019`); six-component interface
contracts and insertion cones (se `0021`).

## Remaining — post-deploy check

After the release carrying se migrations `0019`–`0021` deploys:

1. The live unicycle and the regenerated atomic-mode designs load,
   `view='validate'` runs, and `edit(ops=[{'op':'checkpoint','label':
   'post-deploy'}])` succeeds on one of them.
2. Every cross-reference resolves post uid-migration: `view='drc'` shows
   no new `dangling_*` finding on those designs.
3. Delete this file once 1–2 hold; `pattern-groups.md` and
   `situation-rule-tables.md` then drop their `blocked-by:
   design-state-core`.

## Explicitly NOT in scope

- CoW/structural sharing, result-cache eviction (measure first).
- The optimiser, phase *loop* automation, coupling screen (steps 3–4);
  this item only stores `phase` and `envelope_revision`.
- The situation rule table / three-verdict machinery (step 3) — only
  the schema stub so ids exist.
- Scenario *comparison* at requirements level (step 5).
- Any new MCP kind or verb: everything surfaces through existing se/nm
  ops and views.
- Photoswitch physics payloads (blocktree/photoswitch docs own those).

## Open questions / decisions log

Resolved with Reto 2026-09-12: shared core, DRY (not an se-only
extension, not user-visible); primary id = minted id, labels non-unique
helpers; vocab scenario/situation + state/design-history as proposed;
states machinery shared macro+nano. Remaining (non-blocking, settle in
implementation): exact id form (bigint vs uuid — recommend bigint,
matches house style); whether `situation` stub lands here or waits for
step 3 (recommend: stub here, table in step 3).

- 2026-10-15 (envelope revisions): "tightening" reads as any
  replacement of an envelope on a block that existed before the save,
  including none → set; a new block or a cleared (`null`) envelope mints
  nothing. A cad DSL envelope has no cheap containment test, and the
  revision exists to make staleness detectable — over-minting costs a
  re-score, under-minting trusts a stale result. One revision per
  changed block (so `block_uid` names what tightened), not one per call.
- 2026-10-15 (pin): the spec's `pin(target, value, reason)` becomes an
  se edit op `{'op':'pin','as':<slug>,'reason':R,'ops':[...]}` — the
  pinned change is an op list applied to the copy only, since se has no
  single "value" slot to pin. Headline = blocks, validate error/warning
  counts, BOM `mass`/`unit_cost` only when every bought item is covered;
  `worst_utilisation` stays absent (se computes none yet — absent is
  unknown, never zero). `infeasible` = the first validate error the
  branch has and the parent lacks (rule + subject). A branch inherits
  the scenario, exemptions, envelope revision log and states; NOT the
  parent's checkpoints or revision ledger (its history starts at the
  pin). `switch_branch` needs no verb: a branch is an ordinary design,
  addressed by its slug.
- 2026-10-15 (provenance): se's "params" are its block facets (pose,
  envelope, realized geometry) and its named measures, so the sidecar
  lives in two columns — a `{facet: entry}` sidecar on `se_blocks` and
  one entry per `se_measures` row — both in the core shape. "Every solved
  param" = every number a solver wrote: formfind (`structsolve.form_find`,
  analytic), chain relax (`se.chain.relax`, surrogate), SIMP (`simp/1`,
  full_solve). Absent = no claim; nothing is defaulted onto authored
  numbers, but an authored overwrite of a CLAIMED facet restates it
  (`user_stated`, or `llm_assumed` when the facet's origin stamp is
  `proposed`) so a solver claim never outlives its number; a rebind drops
  the geometry claim. Loads' provenance stays with item 7a.
- 2026-10-15 (cross-design template uid): the spec's "uid→design
  resolver in `resolve_template`" is not needed. A uid is unique per
  design only (branch copies share them), so the identity of a foreign
  block is (design slug, uid); the slug is already in `template_ref`.
  persist resolves both halves with one batched SQL join at save and at
  load, and `precis.blocktree` keeps resolving by the label text it is
  handed — no foreign `load_tree` added to the read path, and the
  blocktree package stays store-free.
- 2026-10-15 (7a): the contract is on the CONNECT (spec `Interface.
  contract`), the cone on the BLOCK (spec `Block.insertion_direction`),
  as the spec places them. Contract loads are in the design frame;
  cone directions in the block frame (rotated by the block's composed
  pose for the check), since a cone travels with its part. "Rejection
  names the missing component" is a validate finding (warn) naming every
  unknown component, not a write-time refusal: se's posture is that a
  partly-known contract is an honest state to save. A connect with no
  contract at all is not flagged — flagging every legacy connect would
  bury the signal; whether undeclared contracts should warn is open.
  The cone intersection is exact for caps ≤ 90° (axes plus pairwise
  boundary crossings as candidates); wider cones are refused.
- 2026-10-15 (item open): whether a connect with NO contract should warn
  once a design opts into contracts (see the 7a entry above). Not
  blocking; settle with the first consumer that reads contracts.

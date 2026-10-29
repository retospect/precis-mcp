---
status: in-progress
title: "design tree: function → decision trees for se/cad agents — remaining slices (DRC, import, red-list review, rerun, scale-out)"
pillar: 3d-design
prio: normal
model: sonnet
---

# design tree — the design knowledge graph, remaining work

Source: gr516958 (conventions storage) and gr516957 (design skills + DRC).

The spec of record was `SPEC.md` in DESIGN's drafts dir; it moved here
2026-10-26, keeping only what is still open. What shipped lives in the
code and its docs:

| slice | shipped as | landed |
|---|---|---|
| S0 schema, validator, import lane | `precis.design_tree`, `precis conventions import --manifest design-tree` | `7646815cd` |
| S1 seed manifests | `src/precis/data/design-tree/` (see its README) | `f69313360` |
| S2 equipment + capabilities | equipment scope, `capabilities.json` + `table_ref`, leaf `implies` | `108c98423` and the S2 branch |
| S3 skill | `precis-se-dtree-help` | `cef15ed25` |
| S4 se hints | `precis_se.dtree_hints`, principles notes, STATUS:done gate | `fa4876f6a` |
| S6 processes | `processes.yaml` | with S1 |
| mechanism leaves | `implies.mechanism` fuse/adhesive/rivet/pin on 10 leaves; `test_packaged_export_pin_leaves` | `feat(dtree): mechanism on pin/fuse/rivet/adhesive leaves` |

- **Rulings** (Reto, 2026-10-25/26, the full list): gr516958 comment 9.
- **Pipeline metrics** (pilot, run 2, run 3): gr516958 comment 10, with
  per-batch tables in
  [design-tree-pipeline/metrics](../runbooks/design-tree-pipeline/metrics/).
- **How the content was made:** the runbook
  [design-tree-pipeline](../runbooks/design-tree-pipeline.md).

## Model in one paragraph (for builders)

A linked graph of mechanical design knowledge that an agent walks per
interface:

- **function trees** (goal → method → leaf; join, rotate and close are
  deep, the rest two levels);
- **process nodes** (DIN 8580; deep for desktop FDM, CNC milling and
  turning, injection moulding);
- **shared libraries:** axes, failure modes, criteria, principles.

Rules hang off nodes and each carries:

- `equipment`;
- `confidence` (standard | handbook | secondary | excerpt-only |
  shop-rule | unverified);
- its source.

Equipment numbers live in `capabilities.json` rows via `table_ref`. Only
standard, handbook and two-source secondary rules are DRC-enforceable;
`conflicts` blocks mark disputed rules unenforceable. The walk backbone
(`specialises`, multi-parent via `also_under`) is a DAG: the validator
rejects cycles, and `related-to` cross-links may cycle but are never
descended.

## Remaining slices

1. **S5 DRC.**
   - Each declared leaf's checks and thresholds come from
     `design_rules.json`. They only add or tighten, never relax global
     defaults.
   - Undeclared leaves get one rolled-up info line per design.
   - Every DRC finding names its rule id.
   - New `unretained_dof` check from `src/precis_se/freedom.py`: every
     DOF of every part is intended or stopped in both directions, and
     friction-only retention under vibration is flagged.
   - Opus reviews the DOF rule (shared with se-design-plan batch 3c).
2. **Import to prod.** No `SPACE:design-conventions` finding exists on
   prod (checked 2026-10-26). Steps: run the importer on the dev DB,
   dogfood one fisheye walk, then prod.
3. **Red-list review.** The red lists (`REVIEW*.md`, 40 + 52 + 167 rows)
   were lost in the /tmp wipe. They are regenerated as a query over the
   manifests: rules with `confidence` shop-rule or unverified, or
   `corrected: true`, that a designer dimensions from. Reto reviews them
   once item 2 has made them findings, through the staging home asked for
   in gr520907 or as a checklist over the imported findings. Each "use X"
   becomes `shop-rule (Reto)`.
4. **S7 rerun.** A fresh se design run with the trees (gr516957 batch 5).
   Each gap becomes a gripe and then a manifest entry. Builders start from
   the three worked walks in
   [worked-walks.md](../runbooks/design-tree-pipeline/worked-walks.md).
   Check their rule ids against the current manifests; S2 split and
   renumbered some.
5. **Scale-out** of the remaining goal roots to deep coverage, on Reto's
   go. Pre-fill goes through the `perplexity-research` /
   `perplexity-reasoning` kinds, never through dumped files (Reto
   2026-10-26); the runbook has the procedure.

## Acceptance criteria

1. S5: a design that declares `params.leaf: join/service/threaded/clearance-tapped`
   on a connect gets that leaf's thresholds checked. A DRC finding names
   `join.yaml#…#c<n>`. A part with a free DOF raises `unretained_dof`.
2. Import: `search(kind='finding', tags=['SPACE:design-conventions','dt:goal'])`
   lists every goal root on prod, and a re-run of the importer is a no-op.
3. Red list: one query returns the red-list rules with node id, value,
   confidence and source. Reto's verdicts are stored in precis, not in a
   file.
4. S7: the rerun's gap gripes link gr516958.

## Explicitly NOT in scope

- PCB design rules, molecular design (hexfold pockets and tunnels), and
  physics method trees. These are later domains, sketched in gr516958
  comment 9 ruling 6.
- A dedicated `dtree` kind. This is revisited at ~1,500 imported nodes,
  if finding search gets swamped.
- Fabrication rules for PCBs.

## Open questions / decisions log

- **[open, non-blocking]** Red-list review home: a staging finding state
  (gr520907) or a checklist over imported findings. Recommend the
  checklist if gr520907 isn't built by the time import lands.
- **[decided]** All design rulings: gr516958 comment 9.

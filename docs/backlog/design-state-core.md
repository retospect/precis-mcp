---
status: ready
title: design core — shared scenarios, provenance, design history, and discrete-states machinery for se + nm
prio: high
model: opus
blocked-by: units-policy-cutover
---

# Design core — shared scenarios, provenance, design history, discrete states

The shared substrate the multiscale spec's new subsystems stand on
(multiscale-design-system-spec.md §1.3–1.6, §5.6; map §Build order
step 2). One internal core package rented by both `se` and `nm` exactly
as both rent the cad kernel — **not a new kind, no user-visible surface
of its own** (Reto 2026-09-12); its capabilities appear only through
the se/nm ops and views.

## Motivation / why

Four subsystems recur identically at both scales and must not be built
twice: (1) scenario/service-environment context that decides which
physics runs at all; (2) per-number provenance that makes margin audits,
fidelity ladders and library-update notification queries rather than
features; (3) **design history** — envelope revisions, checkpoints,
pin→branch — so stale results are detectable and a pin is always
reversible; (4) **discrete states + stimulus-labelled transitions**,
which are true macro AND nano (Howell-style compliant bistables, hard
stops · photoswitches, conformers — Reto 2026-09-12). Blocktree slice 2
becomes the nm *adoption* of (4), not its owner; the macro adoption
enters via the pseudo-rigid-body route on the existing member machinery.

## In scope

1. **Package** `src/precis/design/` + core migrations. Vocabulary per
   the 2026-09-12 rulings: `scenario` = production context; `situation`
   = named swept-volume bundle (schema stub only here — the rule table
   itself is build-order step 3); `state` = physical discrete state;
   the versioning axis is called **design history**, never "design
   state".
2. **Identity — stable `uid`, not row ids.** Today both persist layers
   rebuild row ids on every save (retire-all/reinsert-all) and store
   cross-references (se_connects/se_ports/nm_topology/template_ref) as
   bare name text — "identity is the block name". This item introduces
   a DB-minted stable `uid` (bigint) as PRIMARY identity: minted once
   per block, carried forward across retire/reinsert saves as ordinary
   column data (row ids stay ephemeral and unexposed, honouring nm's
   invariant), preserved by branch copies so branches diff
   block-by-block; new blocks mint new uids. Cross-reference columns
   migrate name-text → uid in the SAME slice (persist load/save keyed
   by uid; names become display labels resolved through the uid map).
   Ops accept uid always; a label resolves when unambiguous, else a
   structured error listing matching uids. Uids are the viewer path
   leaf names. Label uniqueness per design is KEPT for now (live
   `(ref_id, name)` unique index stands — relaxing it is possible once
   nothing references names, logged as a later option, out of scope
   here).
3. **Scenario + ServiceEnvironment** tables: name, quantity,
   objective_weights, lifetime master switch, temperature/chemical/
   vibration/cycle fields, load-case list; presets `prototype`,
   `small_batch`, `mass_production` seeded. A design references one
   scenario. Standard load-case library applied by default,
   explicitly exemptable.
4. **Per-number provenance**: one shared sidecar convention
   ({param: {source, fidelity, solver_id, assumptions,
   library_version?, margin_origin?}}) + helpers/validators in the core;
   contract loads use the SAME enum (the spec's `load_provenance`
   collapses into it — one mechanism).
5. **Design history**: `envelope_revision` int minted on every
   tightening, recorded on every scored result; `checkpoint`/`restore`;
   `pin → branch` with parent_branch_id, one-line reason, headline
   numbers, and the BranchResult comparison (delta vs parent,
   infeasible-what-broke). Branch storage is **naive full copy** via
   the retire-all/reinsert-all persist pattern with uids preserved
   (item 2's carried-forward uid is exactly what makes the copy
   diffable) — CoW structural sharing only when branch count
   measurably hurts (the spec's own naive-first-and-measure rule).
6. **Discrete states machinery** — schema OWNED by blocktree slice 2's
   shape and BUILT by that track as first consumer, in the shared core
   home: `state: (block, name, envelope?, port_pose_overrides?)`,
   `transition: (block, from_state, to_state, driver_kind, driver_ref,
   params)`; **per-block** current state (two independently switchable
   blocks in different states is the photoswitch case — no design-level
   pointer); `driver_kind` stays a CLOSED enum, extended by migration
   with `mechanical` (force/displacement-driven snap-through) for the
   macro adopters alongside {light, reaction, redox, ph, thermal}.
   This item's part is only: verify the macro rental fits (a compliant
   bistable expressible with zero schema change) and that the tables
   live under the core package, not nm-locally.
7. **se/nm adoption — SEPARATELY SHIPPABLE sub-slices** (vet advisory
   accepted): (7a) se adoption — scenario_id, six-component contract
   storage with absent = unknown never zero (Rejection names the
   missing component), insertion-direction cones, provenance on
   params/loads; (7b) nm adoption — scenario_id + provenance. The core
   item itself (1–6) ships with one thin se rental proof, 7a/7b follow
   as their own lands.

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

## Acceptance criteria

- A design created under `prototype` vs `small_batch` stores different
  objective weights and lifetime; validate/DRC output records which
  scenario governed.
- Every solved param on a re-saved se design carries provenance; a
  margin-audit query (all numbers with margin_origin set) returns rows.
- `pin` on a live se design returns a BranchResult with headline +
  delta vs parent; both branches remain fully inspectable; `restore`
  of a checkpoint round-trips.
- A block with two states + a stimulus-labelled transition persists,
  revalidates per state, and the nm side can express an azobenzene
  E/Z pair with it (schema check with blocktree slice 2's tests).
- Label collision: two blocks labelled "strut" → op by label returns
  the structured ambiguity error with both ids; op by id works.
- Envelope revision increments on a tightening write and is present on
  subsequently stored results.
- mypy/ruff clean, `scripts/test --impacted` green, migrations apply on
  a fresh DB and forward from prod baseline.

## Target + blast radius

New `precis/design/` package + core migrations · `precis_se`
persist.py REWRITE (uid-keyed load/save) + handler/validate + plugin
migration converting se_connects/se_ports/notes name-refs to uid ·
`precis_nm` persist.py REWRITE + plugin migration converting
nm_topology/template_ref name-refs to uid · `precis.blocktree`
template_ref resolution · viewer path scheme (uids as leaves) · skills
precis-se-design-help/precis-nm-help. Post-deploy check: live unicycle
+ photonic-arm designs load, revalidate, and accept a checkpoint; all
cross-references resolve post uid-migration.

## Open questions / decisions log

Resolved with Reto 2026-09-12: shared core, DRY (not an se-only
extension, not user-visible); primary id = minted id, labels non-unique
helpers; vocab scenario/situation + state/design-history as proposed;
states machinery shared macro+nano. Remaining (non-blocking, settle in
implementation): exact id form (bigint vs uuid — recommend bigint,
matches house style); whether `situation` stub lands here or waits for
step 3 (recommend: stub here, table in step 3).

## Readiness vet findings (ready-gate, 2026-09-12) — RESOLVED same day

Resolutions: (1) label uniqueness KEPT (live `(ref_id,name)` index
stands); identity moves to a stable carried-forward `uid` and ALL
name-text cross-references migrate to uid in the same slice — the
persist rewrites are now named in Target+blast radius. (2) Stable
identity is the uid column, not row ids — retire-all/reinsert-all row
mechanics stay, nm's "row ids never exposed" invariant honoured; branch
copies preserve uids. (3) States schema defers to blocktree slice 2's
concrete shape (per-block state, closed driver_kind enum + `mechanical`
by migration, no design-level pointer), built by that track in the
shared core home; blocktree plan carries the matching note and both
items now declare their ordering via blocked-by. (4) Adoption split
into 7a/7b sub-slices. Original findings kept below for the record.

- blocker: item 2's "caller labels become... NOT unique" contradicts the
  *live* invariant the persist layer is built on, not just op-dispatch.
  `se_blocks_ref_name_key`/`nm_blocks_ref_name_key` are DB UNIQUE indexes
  on `(ref_id, name)` (`0001_se_kind.sql`, comment: "a block name is
  unique within its (live) design — simpler addressing than per-parent
  scoping"). `load_tree` in both `precis_se/persist.py` and
  `precis_nm/persist.py` keys the whole in-memory tree by name, and
  `se_connects`/`se_ports`/`nm_topology`/`template_ref` all store
  endpoints as bare NAME text with **no block-row FK at all** ("identity
  is the block name" — both persist.py module docstrings). Dropping name
  uniqueness doesn't just need the structured ambiguity error the AC
  describes for op-by-label; it breaks the name-keyed reconstruction that
  connects/ports/notes/template_ref depend on for correctness, with no
  disambiguation path designed. Target+blast radius never names
  `se_connects`/`se_ports`/`nm_topology`/`precis.blocktree` template-ref
  resolution as touched, though non-unique labels force a rewrite of all
  of them.
- blocker: item 2's "Minted ids are the viewer path leaf names" assumes
  ids are a *stable* external identity, but both persist.py modules
  document the opposite: se's save model is "retire-all/reinsert-all...
  **Row ids are rebuilt on every save**"; nm's is stronger still — "row
  ids are never exposed outside this module" and the "Round-2 landmine"
  note is literally about `save_tree` rebuilding every `nm_blocks.id` on
  every save. Item 5 says branch storage reuses this exact retire-all/
  reinsert-all pattern **unchanged**. A viewer leaf name, a per-block
  state row, or provenance keyed to a block id cannot survive a save
  under the pattern as documented, and no persist.py rewrite to make ids
  stable is named in Explicitly-NOT-in-scope or Target+blast radius —
  items 2 and 5 pull in opposite directions on the same mechanism.
- blocker: item 6's states schema doesn't match
  `blocktree-library-build-plan.md` §Slice 2, which is already
  `status: ready` and already concrete: `driver_kind ∈ {light, reaction,
  redox, ph, thermal}` (closed enum) vs. this spec's "open vocabulary:
  wavelength, force, temperature, chemistry"; `state: (block, name,
  envelope?, port_pose_overrides?)` (per-block, no design-level state)
  vs. this spec's "current-state pointer per design" (implies one
  pointer for the whole design, which cannot represent two
  independently-switchable blocks in different states, exactly the
  photoswitch case both docs want). The map (`multiscale-design-
  architecture.md` §Build order, step 2) calls these two "parallel
  tracks... co-designed," but neither file declares `blocked-by` on the
  other, and blocktree slice 2 reads as self-contained and buildable
  today by a different agent with zero reference to a shared core
  package. Nothing stops the two landing with incompatible schemas.
- advisory: item 7 bundles the core package build with two separately
  shippable plugin-adoption slices (se's six-component contract/
  insertion-direction cones/provenance; nm's scenario_id+provenance) —
  each is independently testable once items 1-5 exist and could be split
  out, leaving this item to the core substrate plus one thin rental
  proof.

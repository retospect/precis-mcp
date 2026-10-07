---
status: draft
title: pcb → se binding — consume the 0041 mechanical bridge, one mm→m crossing
prio: normal
pillar: 3d-design
model: opus
---

# pcb → se binding — consume the 0041 mechanical bridge, one mm→m crossing

Design session 2026-09-14 (Reto + agent, glowing-zooming-glade
worktree). Was `blocked-by: nm-se-merge` (window sequencing); that
window ran and shipped the same day — this item is now dispatchable.

## Motivation / why

A board is a real solid in a design: it has a world pose, it must clear
an enclosure, its connectors mate with harnesses. pcb already builds
exactly the projection se needs — `mechanical_profile`
(`precis.pcb.export`): outline, thickness, mounting holes, per-instance
height prisms, self-declared `"units": "mm"`, docstring calling itself
"the 0041 bridge" a cad enclosure references. Its only caller is pcb's
own `view='mechanical'` export builder (`handlers/pcb.py`), which
writes a JSON file for a human to read — **no cad/se code consumes the
output**; the bridge's consumer side was never built. Meanwhile se's
`set_binding` enumerates `cad|structure|component|part` (post-merge
roster) and cannot name a board.

Units ruling (map §Units policy, decided Reto 2026-09-14): pcb is a
**mm enclave**. Its interchange ecosystem — Gerber, Specctra DSN, KiCad,
JLC CPL/BOM, EasyEDA, IPC footprints — is mm/mil-native and
fabrication-facing; converting pcb internals to metres would relocate
one conversion into seven-plus exporter/ingester sites where a scale
error costs real boards. Instead: `mechanical_profile` is pcb's **sole
geometry crossing**. It stays mm (self-declared `"units": "mm"`; the
flexboard item grows it in mm). The se-side derivation converts it
through the two existing unit tables, never a new literal: numeric
values via `precis_se.catalog.to_metres(v, "mm")`, the envelope as a
unit-tagged cad DSL string (`box:w100mmd80mmh1.6mm`) that the DSL
parser converts. The crossing adds no ×1e-3 site anywhere (vet round 3
resolved the Motivation/In-scope contradiction this way).

## In scope

- **`pcb` joins `_BINDING_KINDS`** (`precis_se/ops.py`):
  `set_binding kind='pcb' design=<slug>` — slug-keyed, resolved at read
  time, absence a DRC finding not a write-time rejection (the existing
  binding posture).
- **Read-time derivation, own module** (vet round 1): the
  `precis_se/catalog.py` *pattern* transfers (pure function, no
  store/network access, honest `why_not`, narrow `to_metres`-style unit
  bridge) but its `Derived` type does not — one envelope + one ports
  dict is the wrong cardinality for a board. The new module returns its
  own multi-item shape, and that shape is **a list of segments** (each:
  slab from outline × thickness + per-instance keep-out prisms +
  mounting-hole markers + connector ports), converted at this seam
  through `catalog.to_metres` and DSL unit tokens only. v1 always emits exactly ONE segment — a rigid board is
  the one-segment special case — but the list shape is load-bearing:
  flexboards (Reto 2026-09-14) decompose into segments joined by fold
  lines, and a scalar "the slab" contract would force a re-shape later
  (`pcb-flexboard.md` has the extension path). Guessed prisms carry
  `"origin": "proposed"` as a plain field in that return shape — the
  house vocabulary transfers, but these are read-time-only items, never
  op-authored `node.origins` entries.
- **Wire the derivation into all three `bound_kind` call sites** (vet
  round 1 — read-time resolution is not one hook): `persist.py`'s
  tree-load loop (the component-binding analog; without this,
  `set_binding(kind='pcb')` is write-accepted but never resolved — the
  failure mode that already exists for `bound_kind=='nm'`), `drc.py`'s
  demand check, and `fasten.py`'s clearance gate each get an explicit
  `'pcb'` decision, even where it is "not applicable".
- **Derived type and what clearance consumes** (vet round 3). se
  clearance, DRC, datums, compose and freedom read ONE envelope string
  per block (`ops.effective_envelope` → `node.derived.envelope`), and
  `effective_ports` reads `node.derived.ports`. The new type
  (`BoardDerived`) exposes exactly those two attributes, duck-typed like
  `catalog.Derived`, plus `segments` (the itemised list) and `why_not`.
  - `envelope` (v1) = one conservative `box` enclosing the slab and
    every prism: outline bounding box (w × d) × (thickness + tallest
    top-side prism + tallest bottom-side prism). Exact for a rectangular
    board with no parts, a superset otherwise. Per-prism clearance needs
    compound envelopes, which se does not have — a later item.
  - `segments[0]` carries the exact items: slab (outline polygon,
    thickness), prisms (refdes, x/y extent, height, side,
    `origin: user|proposed`), hole markers, connector ports. The
    proposed-height DRC finding and the views read these.
  - Holes are markers only, never subtracted: no v1 check consumes a
    hole, and subtraction would add kernel cost for nothing.
- **Frame mapping** (vet round 3). In the board block's frame, x/y are
  pcb board coordinates in metres; z = 0 at the bottom copper face, +z
  toward the top layer; bottom-side prisms extend to −z. The builder
  reads pcb's y convention and the cad `box` primitive's anchor from the
  code, maps both with a fixed in-frame offset, and pins the mapping
  with a test (a part at board (10 mm, 20 mm) on top resolves to the
  expected se point).
- **Store access** (vet round 3). The derivation stays pure. A new
  load pass in `persist.py`, mirroring `attach_catalog`, fetches the
  board (`store.pcb_load`, the `pcb_graph` board dict, the features
  list; the builder confirms the exact names), calls the derivation and
  assigns `node.derived`. It is guarded by `hasattr(store, "pcb_load")`
  for fake stores and is total: a missing slug assigns a `BoardDerived`
  with `why_not` set and never raises.
- **`mechanical_profile` gains additive keys** (vet round 3: today each
  block carries only refdes, x, y, layer, height_mm). Each block gains
  `w_mm`/`d_mm` (courtyard extent, falling back to the footprint pad
  bounding box), `rot_deg` and `roles`. Existing keys and values are
  unchanged.
- **Thickness becomes board data**: promote `DEFAULT_THICKNESS_MM`
  from an export-time constant to a per-board column
  `pcb_boards.thickness_mm double precision NOT NULL DEFAULT 1.6 CHECK
  (thickness_mm > 0)` (new core migration, next free number at land),
  set through the existing pcb `stackup` op as a `thickness_mm=`
  argument, carried in the `pcb_graph` board dict, and used by
  `mechanical_profile` (its `handlers/pcb.py` caller passes it) and the
  derivation.
- **se migration widening `se_blocks_bound_kind_check`** (vet round 3:
  the CHECK in `precis_se/migrations/0008_se_drop_nm_tables.sql` allows
  only `cad|structure|component|part`, and NOT VALID still rejects new
  rows, so editing the tuple alone fails at persist). Next free se
  number at land; `se-region-property-layer` slice A takes 0018.
- **Every `bound_kind` reader gets a `'pcb'` decision**, not only the
  three below: `handler.py` status render, `printsolid.py`,
  `printgroup.py`, `ops_export.py` (already generic), `drc.py`
  `mode_binding_mismatch` (silent unless a mode family lists
  `realization_kinds` without `pcb`), and the web routes keyed on
  `structure` (confirm untouched).
- **Height honesty**: instances with `height_mm` absent (JLC
  parametrics often lack it) project as 1 mm prisms carrying
  `origin=proposed`; se validate/DRC enumerates which prisms are
  guesses (filled-fraction honesty, the existing origin vocabulary).
- **Connector classification via a documented `roles` convention**
  (vet round 1 — no classification signal exists today: no
  category/class column, `roles` populated only with thermal/EMC
  values, jlcparts category dropped at ingest): declare `connector` a
  documented value of `pcb_instances.roles` (roles is already the open
  capability set), teach `precis-pcb-help` the convention, and derive
  the board block's se ports from instances carrying it — so
  board↔harness mating is an se connection. Mapping the jlcparts
  category field at ingest to auto-populate the role is a later
  enrichment, not this item.
- **Direction of truth is one-way**: pcb owns board space (placement,
  rotation, routing); se owns the world pose and consumes the
  envelope. se never writes back into pcb placement.

## Explicitly NOT in scope

- **Merging the pcb kind into se** — pcb's IR (nets/footprints/traces/
  layers) is not the block graph; the merge criterion is "shares the
  six-level IR", which pcb fails and nm passed.
- **Converting pcb internals to metres** — the enclave ruling above.
- **3D routing** — routing stays 2.5D layer-graph computation; the
  copper's z-structure is fully determined by the stack.
- **Extruded-copper 3D view** (per-layer trace solids for the viewer /
  enclosure-clearance) — real, later, a derived view; new backlog item
  when wanted.
- **Flexboards** (Reto 2026-09-14: consider) — v1 binds rigid boards
  only, but the contract is shaped so flex *extends* instead of
  rewriting: the derivation's segment-list return (above) becomes
  N segments; fold lines become se **joints** between segment
  sub-blocks (hinge × bend radius — the existing joint vocabulary);
  installed fold angles are se-side state (like world pose — pcb owns
  the flat design and fold-line locations, se owns the installed
  configuration), and multiple install configurations ride the shared
  states machinery from design-state-core. Fabrication stays flat mm
  2.5D, so the enclave ruling and the single ×1e-3 crossing are
  untouched. pcb-side prerequisites (board_type, fold features,
  flat-domain bend DRC) → `pcb-flexboard.md`.
- **Boundary-unit ergonomics for pcb inputs** (`parse_quantity` so
  agents can author "8 mil"; `format_quantity` display) — compatible,
  incremental, not this item.
- **Two-way sync or se-side placement DRC** — pcb DRC owns nets,
  courtyards, layer legality; se checks only the projected solid
  against the world. Two-way *negotiation* (se-authored outline
  linked to the board; place/route failure emits an area demand; se
  grows the proposed envelope) is the designed path and is NOT sync —
  it's message-passing over owned data → the "Absorbed 2026-09-26"
  section at the end of this file.

## Acceptance criteria

- `set_binding(kind='pcb', design=<slug>)` accepted; unknown slug is a
  `pcb_binding_unresolved` DRC warning naming the slug, not an op error.
- se clearance/validate sees the board at metre scale: a test-DB board
  built by a fixture with a 100 mm × 80 mm rectangular outline reads
  envelope width 0.1 m (test pins the exact value; the "2000 mm tube
  becomes a 2000 m one" failure class is the target).
- No scale literal in the derivation module: a test reads its source
  and fails on `1e-3`, `0.001`, `1e3`, `1000` or `/ 1000` as numeric
  literals; conversion goes through `catalog.to_metres` and DSL tokens.
- Absent-height instances appear as 1 mm `origin=proposed` prisms and
  are enumerated by a `pcb_height_proposed` DRC warning listing the
  refdes.
- The frame-mapping test above passes.
- `thickness_mm` round-trips as board data; `mechanical_profile` and
  the export view use it; default stays 1.6. The new column is an
  independent scalar — the reserved per-layer `stackup[].thickness_mm`
  (unpopulated today) stays out of scope; reconciling the two is a
  later item if stackup ever fills in.
- A bound board resolves at tree load: `set_binding(kind='pcb')` on a
  real dogfood board yields derived geometry visible to
  validate/clearance without further ops (test-pinned — guards the
  write-accepted-never-resolved failure mode).
- Instances carrying the `connector` role yield board-block ports named
  by refdes (`J1`); a `connect` from another block's port to `J1`
  survives tree round-trip and resolves after re-derivation (derived
  ports are re-derived at load, never stored).
- pcb-side behaviour otherwise unchanged: Gerber, DSN, KiCad, JLC and
  EasyEDA exporters untouched; `mechanical_profile` changes are
  additive keys only (the enclave boundary holds).

## Target + blast radius

Two passes; A blocks B (vet round 3 split).

- **Pass A, pcb side:** `pcb_boards.thickness_mm` core migration ·
  `stackup` op `thickness_mm=` · `pcb_graph` board dict ·
  `precis/pcb/export.py` (`mechanical_profile` thickness + additive
  per-block keys) · its `handlers/pcb.py` caller · `precis-pcb-help`
  (projection note + the `connector` roles convention). Regenerate the
  schema baseline and `docs/reference/schema.md` via `scripts/bump`.
- **Pass B, se consumer:** se migration widening the `bound_kind`
  CHECK · `precis_se` ops (`_BINDING_KINDS`) / **persist** (load pass) /
  drc / **fasten** / handler / printsolid / printgroup · the new
  derivation module · `precis-se-help` gains the binding. Pass B's
  prisms need pass A's extents.

The map §Units policy wording (`multiscale-design-architecture.md`) is
already amended; nothing to do there.

## Open questions / decisions log

- **Decided** (Reto 2026-09-14): pcb binds rather than merges; mm
  enclave with `mechanical_profile` as sole crossing; 1 mm proposed
  default height; thickness is real board data.
- **Decided** (Reto 2026-09-14, "consider flexboard"): v1 stays
  rigid-only but the derivation contract is segment-shaped (list, v1
  emits one) so flex extends without a re-shape; flex itself trails in
  `pcb-flexboard.md` (fold lines = se joints, install state se-side).
- **Decided** (agent call 2026-09-14 post-vet, Reto may veto):
  connector classification = a documented `connector` value in
  `pcb_instances.roles` (the open capability set; no new column, no
  ingestion change). jlcparts-category auto-population is a later
  enrichment. Resolves the former open question, which the vet showed
  posed a choice between two mechanisms that both didn't exist.
- **Resolved 2026-09-14** (same session, post-vet): all three vet
  blockers folded back into the sections above — the derivation gets
  its own multi-item return shape (pattern-reuse, not `Derived`
  type-reuse), the three `bound_kind` call sites incl. `persist.py`'s
  load loop are named in scope and blast radius with a load-time
  acceptance criterion, and the connector signal is now the decided
  roles convention. Motivation's "zero callers" corrected to
  "no cad/se consumer". Thickness scalar declared independent of the
  unpopulated `stackup[]` per-layer field.
- **Decided** (agent calls 2026-10-02, se-machine-design, Reto may
  veto), folding readiness vet round 3: holes are markers in
  `segments`, never subtracted (closes the former open question); no
  new ×1e-3 site — conversion through `catalog.to_metres` + DSL unit
  tokens, `mechanical_profile` stays mm; v1 envelope = one conservative
  box over slab + prisms, with itemised `segments` beside it; thickness
  set through the `stackup` op; findings `pcb_binding_unresolved` and
  `pcb_height_proposed`, both warn; two build passes, pcb side first.

- **Accepted** (Reto 2026-10-02, review queue se-machine-design-2): all
  five round-3 calls stand for v1. Reto adds that the model must get
  better; that is the ranked follow-up below.

## Follow-up v2 — real per-part heights and subtracted holes (ranked, Reto 2026-10-02)

Not v1, but not optional either: Reto ruled the one conservative box
and marker-only holes acceptable **for now**, and wants the model to
grow to:

1. **Per-part envelopes.** Each placed part is its own solid with its
   real height (courtyard/footprint extent × `height_mm`, top or bottom
   side), so clearance sees the actual skyline instead of a box at the
   tallest part. Needs se to carry more than one envelope per block:
   either derived child blocks (one per part, read-time, never
   op-authored) or a compound envelope the clearance check iterates.
   The v1 `segments[0].prisms` list already holds the data, so this is
   a consumer change, not a new derivation.
2. **Mounting holes subtracted** from the slab solid, so a fastener or
   standoff passing through the board clears it in se clearance and
   fastener-access checks (`se-mechanical-drc.md`). The v1 hole markers
   carry position and diameter already.
3. **Proposed heights still flagged.** A part without `height_mm` keeps
   the 1 mm `origin=proposed` prism and the `pcb_height_proposed`
   warning; per-part envelopes make that guess more visible, not less.

Ranked in `threads/se-machine-design.md` directly after this item.
Open when picked up: which of the two multi-solid shapes (child blocks
versus compound envelope) se adopts. That call is shared with
`cross-scale-single-assembly.md`, which needs the same capability for
cartridges.

### Readiness vet round 3 (se-machine-design, 2026-10-02)

Verdict not-ready, five blockers, all folded into the sections above:
no se migration for the `bound_kind` CHECK; no stated path from
segments to the one envelope string clearance reads; the ×1e-3
location contradiction; `mechanical_profile` lacking extent, rotation
and roles; store access for the pure derivation unstated. Advisories
folded: thickness plumbing, the other `bound_kind` readers,
`mode_binding_mismatch`, tighter acceptance criteria, `model: opus`,
stale pointers. Round 1's "single write-time gate" confirmation below
was wrong — the se CHECK is a second gate. Stale in the sibling item,
not fixed here: `cross-scale-single-assembly.md` still calls this item
"in flight" and cites `handlers/pcb.py:1826` (the caller is now
~1981).

### Readiness vet (glowing-zooming-glade, 2026-09-14)

- **blocker** — the "Read-time derivation (the `precis_se/catalog.py`
  Derived pattern)" bullet cites a pattern shaped for the wrong
  cardinality. `catalog.py`'s `Derived` dataclass
  (`envelope: str | None`, `ports: dict[str, PortSpec] | None`,
  `why_not`, `specs`) yields exactly ONE envelope + ONE ports dict per
  call — one bought part, one bounding volume. A board profile is
  inherently multi-item (one slab + N per-instance keep-out prisms + N
  mounting-hole markers + N connector ports from one binding). Reusing
  `Derived` literally means forcing that into a single
  envelope/ports pair, which loses the per-instance/per-hole honesty
  the rest of the item depends on (absent-height prisms individually
  tagged `origin=proposed`, holes individually enumerable). The
  *pattern* (pure function, no store/network access, honest `why_not`,
  `to_metres`-style narrow unit bridge) transfers; the concrete
  `Derived` type does not — the new derivation module needs its own
  richer return shape (a small tree/list of sub-items), not a
  `precis_se.catalog` import.
- **blocker** — "which pcb instance classes count as connectors for
  port derivation (component class metadata vs an explicit flag on the
  instance)" poses a choice between two options, but neither exists
  today: `pcb_components` (migration `0047_pcb_kind.sql`) has no
  category/class column, only a free-text `label`; `pcb_instances` has
  a free-form `roles text[]` (populated today with
  sensitive/noisy/hot/temp-sensitive — thermal/EMC roles, no
  connector-related value in use anywhere); and
  `precis/pcb/catalog.py::normalize_jlcparts_row` doesn't map or store
  any upstream jlcparts category/subcategory field (`package`,
  `height_mm`, `params`, `description` — no `category`). A repo-wide
  grep for `"connector"` as a stored value/column returns zero hits
  outside prose comments. The connector-ports in-scope bullet and its
  acceptance criterion ("Connector instances yield board-block ports")
  have no data to dispatch on without adding a classification signal
  first (a new column, a new ingestion mapping, or a documented
  `roles` convention) — that addition isn't named in scope or blast
  radius.
- **blocker** — read-time `bound_kind` resolution is not one dispatch
  point to extend; it's three separate ad hoc call sites:
  `persist.py`'s `attach_catalog`-equivalent tree-load loop (the exact
  mechanism a `Derived`-populating pass for `bound_kind == 'component'`
  already uses, at `persist.py` lines ~237-249 — the actual analog for
  where a `'pcb'` derivation would need to run at load time),
  `drc.py`'s bought-part demand check (`bound_kind in
  ("component", "part")`), and `fasten.py`'s clearance gate
  (`bound_kind != "component"`). "Target + blast radius" names
  ops/validate/drc/handler + the new derivation module but not
  `persist.py` — the file that actually populates `node.derived` at
  load time. A coder could ship the derivation module and never wire
  it into the tree-load path, leaving `set_binding(kind='pcb', ...)`
  write-accepted but never resolved (the same failure mode the vet
  found already exists for `bound_kind == 'nm'`, which also has zero
  `persist.py` read-time hits today).
- **advisory** — "It has zero code callers" (Motivation) is one call
  short: `precis/handlers/pcb.py:1352` calls `mechanical_profile` from
  the handler's own `view='mechanical'` export builder. The substantive
  claim holds (no *downstream* cad/se consumer reads the output — it's
  only ever written to a JSON export file for a human/agent to read),
  but "zero" is not literally true.
- **confirmed, no issue** — claim re thickness: `pcb_boards`
  (migration `0138_pcb_boards_routes.sql`) is a real board-config row;
  a scalar `thickness_mm` column doesn't exist yet (only a
  schema-legal-but-unpopulated per-layer `stackup[].thickness_mm`), so
  promoting `DEFAULT_THICKNESS_MM` genuinely needs a new column — and
  "Target + blast radius" already names "pcb board-field migration",
  so this is correctly scoped, not a gap. Minor ambiguity worth a
  one-liner: whether the new board-level field is independent of (vs.
  derived by summing) the already-reserved per-layer `stackup[]`
  thickness — not blocking.
- **confirmed, no issue** — `origin` (`user`|`proposed`) is a real,
  established house vocabulary (`precis_se/ops.py::ORIGINS`,
  `_stamp_origin`, `SeBlock.origins`; `freedom.py` already walks
  `origins` reporting `proposed` facets; `formfind.py` already writes
  solved poses back stamped `origin='proposed'`), so the 1 mm-default
  prisms proposal has real precedent to point at. Caveat: the derived
  prisms here are read-time-only (never an op-authored `se_blocks`
  facet), so they'd carry `"origin": "proposed"` as a plain field in
  the new derivation module's own return shape, not a literal
  `node.origins[...]` entry — the vocabulary transfers, the storage
  mechanism doesn't apply 1:1. Not blocking, worth a one-line
  clarification in-scope.
- **confirmed, no issue** — `_BINDING_KINDS` (`precis_se/ops.py`) is
  confirmed the single write-time gate (`("cad", "nm", "component",
  "part")`); adding `"pcb"` there is exactly the one-line change the
  in-scope bullet describes.

### Readiness vet round 2 (glowing-zooming-glade, 2026-09-14)

All 3 round-1 blockers resolved (derivation is its own pattern-reuse
multi-item module; connector classification is a `pcb_instances.roles`
value — confirmed no `CHECK` constraint blocks it, so no migration
needed; all three `bound_kind` call sites incl. `persist.py`'s load
loop are in scope/blast-radius/acceptance-criteria) and the advisory is
corrected (Motivation now names the one real caller). No new
inaccuracy found in the revision.

---

# Absorbed 2026-09-26

## se↔pcb outline negotiation — grow the box when place/route fails (idea)

_Grouped 2026-09-26; was `pcb-se-negotiation`._

Reto, 2026-09-14 (glowing-zooming-glade design session): folding breeds
two-way constraints — the placer and router see via/fold keep-out zones
that come from mechanical intent, and mechanical space depends on what
placement needs. Wanted: author the board outline in se and *link* it
to the pcb design (or author in pcb and mirror to se); when routing or
placement fails, pcb should be able to ask for more area and se should
"make the box bigger". Can it be done? Yes — with owned-data message
passing, not two-way sync; nearly all machinery is shipped.

**The flows, each with one owner:**

- **Intent, se→pcb**: se authors the outline as a proposed envelope
  (flat pattern; `origin=proposed` — an envelope is a *budget*, se's
  whole design language is suggestive-hardening) and, for flex, fold
  lines as joint positions between segments. Exported to pcb as its
  authored `outline` feature + fold/keep-out features. Crossing: the
  **same seam module** as `mechanical_profile`, opposite direction
  (m→mm). This refines the map's enclave ruling: one seam *module*
  owns both directions of the pcb crossing — still exactly one place
  in the tree where the 1e-3/1e3 pair appears.
- **Realization, pcb→se**: `mechanical_profile` as already specced in
  `pcb-se-binding.md` (segments, prisms, holes, connector ports).
- **Failure, pcb→se, as findings not writes**: on place/route failure
  the pcb side emits a *computed demand* — "infeasible at this
  outline; shortfall ≈ X mm² / escape capacity exceeded on edge Y"
  (escape.py's capacity math and the optimizer's objectives can ground
  the number). pcb owns the feasibility verdict; it never writes se
  state.
- **Growth is an ordinary se op**: the driving agent (later a
  negotiation job) enlarges the proposed envelope via `set_envelope` —
  and se DRC immediately re-checks the grown box against its
  neighbours, which is exactly the check se exists to run. "Can the
  box get bigger" is answered by the surrounding design: the freedom
  vocabulary and clearance findings say whether the slack exists, and
  if it doesn't, the conflict surfaces to the human instead of being
  silently absorbed.
- **The loop**: propose outline → link (the shipped `link` verb —
  designs are in the ref graph) → pcb place/route attempt → demand
  finding → grow envelope → re-export → retry. Converges, or
  terminates with a genuine spatial conflict made visible.

**What stays banned** (per `pcb-se-binding.md`): two-way *sync*. Every
datum keeps one writer — outline intent: se; placement/routing/
feasibility: pcb; world pose + installed fold state: se. Negotiation
is messages over owned data, never two writers of one field.

**New pieces needed** (beyond `pcb-se-binding.md` +
`pcb-flexboard.md`): the se→pcb outline/fold export in the seam
module; the placer/router area-demand finding; agent-driven loop first,
a negotiation job type only if the manual loop proves the shape.

test: dogfood loop — a deliberately undersized se outline linked to a
board whose escape capacity fails; the demand finding names the
shortfall; one `set_envelope` growth; re-run places and routes clean;
se DRC confirms the grown box still clears its neighbours.

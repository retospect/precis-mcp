---
status: ready
title: se walker — foothold-occupancy states with per-state relaxed poses, light transitions, cursor geometry, spectral channel budget DRC, make-tree protocol
prio: high
model: opus
---

# `se` walker — a DNA walker on an origami track as discrete states driven by light

## Motivation / why

Research target: a walker stepping along an origami track where each
wavelength (a) moves the cursor one station and (b) fires one self-terminating
addition at the cursor — colours as the instruction set. The state machinery
exists (`src/precis/design/states.py::BlockState/Transition`, `DRIVER_KINDS`
has `light`, `driver_ref` = wavelength; ops `declare_states`/
`declare_transitions`/`set_current_state`; `view='sweep'`), and
`docs/backlog/photoswitch-states-and-spectral-dof.md` already rules that
light-driven motion is N rigid states + labelled transitions and asks for a
spectral-channel-budget DRC. What is missing: a state today carries only an
envelope override and per-port pose deltas (`handler.py::_apply_state_arg`,
transient and store-free; `_STATE_COLS` has no pose); a walker state is
**which foothold each leg occupies**, and the body pose per state is a
*derived* number that must be stored per state — `se_blocks.pose_xyz` is one
slot, and re-running the relax at every `get(state=)`/sweep combination is
not affordable.

## In scope

- **Occupancy + derived pose on states**: core migration adding
  `design_states.occupancy jsonb` (`{"<strand>.<ord>": "<helix>@<offset>" |
  null}`) and `design_states.pose jsonb` — the **owning block's own** pose
  in that state (`{xyz, rot}` — no `origin` key inside the value: the slot is
  machine-derived by construction, only `set_state_pose` writes it, and
  `view='block'` labels it `proposed` the way `node.origins[facet]` is
  labelled; `design_states` is keyed
  `(ref_id, block_uid, name)`, per block never per design, so a state never
  carries another block's pose; parts that move with the walker are its
  children, whose se poses are parent-relative). Written only by a new
  `states.py::set_state_pose`, called from `relax_chain(state=)`; the
  `set_states` upsert driven by `declare_states` leaves `pose` untouched
  (`COALESCE(EXCLUDED.pose, design_states.pose)`), so re-declaring stations
  never wipes relaxed poses. `BlockState.occupancy`, `BlockState.pose`;
  `_STATE_COLS`; vetting in `ops.py::_op_declare_states` (occupancy only — a
  `pose` in the payload is rejected). Semantics: occupancy
  **overrides** the leg domain row's `(helix, forward, start, end)` for that
  state; `null` = free leg, exempt from `chain_dangling_domain` and from
  pairing in that state. `derive_pairing(tree, state=)` honours it;
  `relax_chain(state=)` settles the body and writes the per-state pose;
  `_apply_state_arg` applies `pose` first, then envelope and port deltas.
- **Body pose mechanism** (how "relax settles the body" works, no new
  solver): the walker body is one more **rigid body** in
  `precis_chain.relax.relax_bundle` (bodies = capsule + two end frames, the
  same object a helix segment is). Each leg is a strand: its foot domain,
  when occupied, is **pinned** to the foothold's backbone exits
  (`fibre.backbone_exit` of the track helix at the occupied offset — the
  `pins` argument); its body-side end is tied to a named attachment site on
  the body by a **loop spring** at the leg's free-nucleotide contour
  (`loop.py`'s `(n+1)·c` convention, the `loops` argument). A `null` leg
  keeps its spring and loses its pin. The body's frame after descent is the
  per-state pose that `set_state_pose` stores; `chain_loop_short` at a
  station is a leg whose contour cannot span site→foothold in that state.
  the chain domain's `relax_chain` already assembles bodies/hinges/loops/
  pins for helices and strands; this item adds the body as a body and the
  legs' pins from occupancy — nothing else in the solve changes.
- **Sweep**: `_render_sweep` iterates states, applies per-state poses, runs
  `envelope_overlaps` for chain-vs-other pairs AND `chain_clash` per
  combination (segment↔segment is excluded from the SDF scan); no memo
  across states (A9). A state with no stored pose reports
  `chain_state_unrelaxed` instead of guessing.
- **Sugar op** `declare_stations(walker, legs, footholds)` → N states +
  transition skeleton (forward chain, reverse edges with their own driver).
- **Transitions**: `driver_kind='light'`, `driver_ref` = wavelength or `rxn`
  slug; `params` read from `material` rows (`pss_short_fraction` at that
  wavelength, `thermal_half_life`). **Ratchet guard** = new key
  `Transition.params.guard` — an occupancy predicate over the *from* state
  (`{"leg_b": "bound"}`); `params` is unconstrained jsonb in 0162 and
  `_op_declare_transitions` only requires a dict. Read **handler-side** (next
  to spectral; transitions are not on the tree, they come from
  `design_states.transitions_for(store, ref_id, uid)`) as
  `chain_transition_guard` when the from-state violates it.
  `Transition.requires` keeps its compose-target meaning and is not touched.
- **Cursor geometry**: per state, world pose of the walker's attachment site
  (a `sites` port from `realize_chain` — `n<k>_c5m`/`n<k>_maj`/`n<k>_min` at
  offset `k`) vs track surface and a named feedstock zone →
  `view='stations'` (registered in `_VIEW_ARGS`).
- **Spectral budget** `precis_se/chain/spectral.py`, handler-side (reads the
  store): every light transition and every `rxn` step in the protocol
  contributes an absorption band (`material` rows `lambda_max`, `fwhm`,
  conditions); pairwise overlap → `chain_spectral_crosstalk` warn; more
  independently-addressed channels than the design's authored
  `chain_channels_available` → `chain_channel_budget` error, number quoted.
  This is the DRC `photoswitch-states-and-spectral-dof.md` asks for.
- **Protocol** `precis_se/chain/protocol.py::make_steps(tree, store)` → a
  `make` tree (`src/precis/handlers/make.py`), one step per transition or
  addition, `meta={illuminate:{wavelength_nm, duration_s}, station, rxn}`;
  consumed by the zone compiler in `docs/backlog/ewod-synthesis-protocol.md`.
- Dogfood: rectangle from the chain domain + 3 stub-helix footholds + biped
  walker (two azobenzene-gated leg domains).

## Explicitly NOT in scope

- The addition chemistry itself: reactions are `rxn` rows, products are
  `make` outputs; this item owes geometry and the step list only.
- Droplet choreography (→ `ewod-synthesis-protocol`).
- Kinetics of stepping, yield per step, fatigue — reported from `material`
  rows if present, never computed.
- Absorption spectra computation (→ attached-models layer); this item reads
  band rows.
- Authored per-state poses; a state's pose is only ever the relax's output.
- Non-spectral addressing (polarisation, two-photon) — named in the budget
  finding's text, not modelled.

## Acceptance criteria

- One `put` builds track + walker + 3 stations; `view='chain'` lists 3
  footholds single-occupied in state 0 and one paired per state thereafter;
  a free leg (`null`) raises no `chain_dangling_domain`.
- `relax_chain(state=)` for each station stores the walker's per-state pose;
  `get(args={'state': …})` returns poses that differ between stations without
  re-running relax (spy on the name the handler binds,
  `precis_se.chain.relax.relax_bundle`, zero calls on read); a re-run of
  `declare_stations` leaves the stored poses intact; an unrelaxed state
  reports `chain_state_unrelaxed`.
- `view='sweep'` over 3 states runs `envelope_overlaps` and `chain_clash`
  3× each (spy) and reports no clash; a deliberately short leg fails the far
  station only, as `chain_loop_short`.
- Transitions `st0→st1` (`light`, `405nm`) and `st1→st2` (`light`, `365nm`)
  plus reverse edges render in `view='block'`; a transition whose
  `params.guard` is violated by its from-state → `chain_transition_guard`.
- `make_steps` emits 2 ordered steps with wavelengths in `meta`, linked to
  the design.
- Spectral: 6 channels used with `channels_available: 4` →
  `chain_channel_budget` error quoting 6 > 4; two bands with λ_max 20 nm
  apart and FWHM 40 nm → `chain_spectral_crosstalk`.
- A9: no cache key anywhere in the new code omits the state.

## Target + blast radius

Core migration (`design_states` two columns), `src/precis/design/states.py`
(`_STATE_COLS`, `set_states` upsert must preserve `pose`, new
`set_state_pose`, `BlockState`), `precis_se/ops.py`, `handler.py`
(`_apply_state_arg`, `_render_sweep`, `_VIEW_ARGS`, new `view='stations'`),
`precis_se/chain/{pairing,relax hook,spectral,protocol,drc}`, `make` kind as
consumer, skill `precis-se-chain-help` §walker, pointer line in
`photoswitch-states-and-spectral-dof.md` (done). Web `design_turn` allowlist
picks up new pure ops via `known_ops`; `relax_chain` stays handler-level.
Also touched, because `state=` has to reach them: `precis_se/atomic/apply.py`
(`HANDLER_LEVEL_OPS` is where `relax_chain`'s op payload is parsed — the
`state` key is read there and passed down), `precis_se/drc.py` (threads
`state` to `chain/drc.py::findings` so `chain_loop_short` /
`chain_dangling_domain` are per-station) and `handler.py::_STATE_VIEWS`
(`view='drc'` joins the state-aware views for the chain findings only; the
non-chain rules stay state-blind, as the code comment there anticipated).
**Delivered by the chain domain (2026-09-29), not built here:** `derive_pairing(tree,
state=None)` and `relax_chain(…, state=None)` carry the kwarg from day one as
a no-op when `None`; this item only fills it in. Their signatures are
recorded in that spec.

## Open questions / decisions log

- 2026-09-27 station pose derived by relax, not authored. Decided.
- 2026-09-27 (review) derived pose must be STORED per state
  (`design_states.pose`, `origin='proposed'`): one `se_blocks.pose_xyz` slot
  cannot hold N stations and re-relaxing per read blows the sweep budget.
  The earlier rejection of a state pose field conflated authored with
  derived. Decided.
- 2026-09-27 (review) ratchet is `params.guard`, a new handler-side reader;
  `requires` is compose's target field and stays. Decided.
- 2026-09-27 (review 2) `pose` is the owning block's own pose, never a
  cross-block map (`design_states` is per block); written by a dedicated
  setter so the `declare_states` upsert cannot wipe it. Decided.
- 2026-09-27 spectral budget lives here, not in the chain domain. Decided.
- 2026-09-28 a state may swap the walker's envelope (bound vs free foot)
  through the existing `BlockState.envelope`; no work in this item, the
  sweep already applies it. Decided.
- 2026-09-28 (ready gate) blocker: Target + blast radius never names
  `precis_se/atomic/apply.py` (`HANDLER_LEVEL_OPS`, currently
  `("bind_structure","unbind_structure","generate","realize")`, each op
  hand-dispatched, no generic kwarg pass-through) even though In-scope
  requires `relax_chain(state=)` — the new kwarg has to be threaded through
  wherever `relax_chain`'s handler-level dispatch parses its op payload,
  almost certainly that file. Also never names the core `precis_se/drc.py`
  (calls `chain/drc.py::findings`) — `chain_loop_short`/
  `chain_dangling_domain` need a state path too for the per-station sweep
  acceptance criteria to be checkable, and `_STATE_VIEWS`/`_VIEW_ARGS`
  (`handler.py` ~4239-4243) today deliberately excludes `view='drc'` from
  `state=` ("a later round"). As written a builder following only the
  Target list will miss these edits.
- 2026-09-28 (ready gate) blocker: the mechanism deriving the walker's own
  rigid pose from leg-domain occupancy is asserted, not specified.
  the chain domain scopes `relax_chain` as "segment rigid bodies + hinges +
  loops + crossover pins" over chain (helix/strand) blocks only; nothing
  there or here says how a non-chain block (the walker) gets a rigid pose
  registered off two foothold contact points. `formfind.py` (se's other
  solver-writes-`origin='proposed'`-pose precedent) is a truss
  force-density solve, not applicable here. This is the load-bearing
  physics of the whole item and needs a named mechanism before a builder
  can implement "relax_chain(state=) settles the body".
- 2026-09-28 (ready gate) blocker: the chain domain's own spec defines
  `derive_pairing(tree)` with no `state=` param and never mentions
  `relax_chain(state=)` at all — this item's In-scope assumes both gain a
  `state=` kwarg. Target does list `precis_se/chain/pairing.py` (so
  `derive_pairing`'s extension is at least owned somewhere), but never
  `atomic/apply.py` for `relax_chain` (see line above) — what must land in
  the chain domain vs. what this item itself extends is not stated.
- 2026-09-28 (ready gate) advisory: the acceptance criterion "`view='chain'`
  lists 3 footholds single-occupied in state 0 and one paired per state
  thereafter" presupposes `view='chain'` (shipped in the chain domain) either
  accepts `args={'state':...}` (needs `_STATE_VIEWS`/`_VIEW_ARGS`
  registration, not in either spec's Target) or renders a per-state
  occupancy table unprompted — unstated which; a reader could build either.
- 2026-09-28 (ready gate) advisory: `declare_stations(walker, legs,
  footholds)` names the op and its rough output (N states + transition
  skeleton) but not the `legs`/`footholds` argument shapes or the gait rule
  (which leg moves at which station, hand-over-hand vs simultaneous) —
  underspecified enough that two readers build different steppers.
- 2026-09-28 (ready gate) advisory: the occupancy value format
  `"<helix>@<offset>"` presumes single-offset (1-bp) addressing;
  the chain domain's build log had this OPEN ("per-position geometry
  overrides vs a 1-bp domain" — proposed, not decided). This item's
  addressing scheme may not be well-defined by the time it starts.
- 2026-09-28 (ready gate) advisory: `design_states.pose` embeds
  `origin:'proposed'` inside the jsonb value itself; every existing se
  pose-provenance case (`node.origins[facet]`, `ops.py:897-914`,
  `formfind.py:324`) keeps `origin` as a sibling dict entry, not inside the
  value. New shape, not reconciled with the established convention, and
  nothing states what stops a write from stamping `origin:'user'` on a
  machine-derived slot.
- 2026-09-28 (ready gate) advisory: the "OPEN: whether a state may also
  swap the walker's envelope" item above is still open and unresolved — low
  severity since `BlockState.envelope` needs no schema change either way,
  but per `TEMPLATE.md` no open item should remain when flipped to
  `status: ready`.
- 2026-09-28 (ready gate) split signal, not a blocker: the item bundles at
  least 3 loosely-coupled deliverables — (a) occupancy+derived-pose+sweep
  infra (the two blockers above sit entirely here), (b) the spectral
  channel-budget DRC (`chain_channel_budget`/`chain_spectral_crosstalk`,
  needs only `Transition` + `material` rows), (c) `make_steps` (needs only
  declared transitions). (b) and (c) don't touch occupancy/pose/relax at
  all and could ship once transitions + the ratchet guard exist, without
  waiting on the pose-derivation mechanism gap above to be resolved.
- 2026-09-28 (post-gate) the three blockers above are resolved in the body:
  Target now names `atomic/apply.py`, `precis_se/drc.py` and `_STATE_VIEWS`;
  the body-pose mechanism is the new In-scope bullet (walker body = one more
  rigid body in `relax_bundle`, foot domains pinned to `backbone_exit`, legs
  as loop springs at `(n+1)·c`, `null` = spring without pin); the `state=`
  kwargs on `derive_pairing`/`relax_chain` were delivered by the chain domain and
  recorded in its spec. Decided.
- 2026-09-28 (post-gate) advisories: `view='chain'` joins `_STATE_VIEWS` and
  renders occupancy for the requested state (no state → the domain rows as
  declared). `declare_stations(walker, legs, footholds)`: `legs` = ordered
  list of strand block names (rear→front), `footholds` = ordered list of
  `"<helix>@<offset>"`; gait is hand-over-hand — station *i* has leg *j*
  bound at foothold *i+j*; transition *i→i+1* lifts the rear leg and rebinds
  it at foothold *i+L*, its reverse edge the inverse; other gaits are
  authored with `declare_states` directly. Occupancy `"<helix>@<offset>"`
  is the foot domain's **start** offset; its length comes from the domain
  row, so the addressing does not depend on 1-bp overrides. `pose` value is
  `{xyz, rot}` with provenance implied by the writer (In-scope). Rule names
  are flat snake_case `chain_*` (house convention; the dotted spelling was a
  draft artefact). Decided.
- 2026-09-28 `blocked-by` moved from the chain domain to
  `se-nucleic-realize-export` (split off it the same day): cursor geometry
  reads the `sites` ports only `realize_chain` mints. Decided.
- 2026-09-30 blocker cleared: se-nucleic-realize-export shipped
  (realize_chain, sites ports n<k>_c5m/_maj/_min, view='export'). Decided.

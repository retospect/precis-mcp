---
status: draft
title: se walker — foothold-occupancy states with per-state relaxed poses, light transitions, cursor geometry, spectral channel budget DRC, make-tree protocol
prio: high
model: opus
blocked-by: se-nucleic-acid
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
  in that state (`{xyz, rot, origin:'proposed'}`; `design_states` is keyed
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
  state; `null` = free leg, exempt from `chain.dangling_domain` and from
  pairing in that state. `derive_pairing(tree, state=)` honours it;
  `relax_chain(state=)` settles the body and writes the per-state pose;
  `_apply_state_arg` applies `pose` first, then envelope and port deltas.
- **Sweep**: `_render_sweep` iterates states, applies per-state poses, runs
  `envelope_overlaps` for chain-vs-other pairs AND `chain.clash` per
  combination (segment↔segment is excluded from the SDF scan); no memo
  across states (A9). A state with no stored pose reports
  `chain.state_unrelaxed` instead of guessing.
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
  `chain.transition_guard` when the from-state violates it.
  `Transition.requires` keeps its compose-target meaning and is not touched.
- **Cursor geometry**: per state, world pose of the walker's attachment site
  (a `sites` port from `realize_chain`) vs track surface and a named
  feedstock zone → `view='stations'` (registered in `_VIEW_ARGS`).
- **Spectral budget** `precis_se/chain/spectral.py`, handler-side (reads the
  store): every light transition and every `rxn` step in the protocol
  contributes an absorption band (`material` rows `lambda_max`, `fwhm`,
  conditions); pairwise overlap → `chain.spectral_crosstalk` warn; more
  independently-addressed channels than the design's authored
  `chain.channels_available` → `chain.channel_budget` error, number quoted.
  This is the DRC `photoswitch-states-and-spectral-dof.md` asks for.
- **Protocol** `precis_se/chain/protocol.py::make_steps(tree, store)` → a
  `make` tree (`src/precis/handlers/make.py`), one step per transition or
  addition, `meta={illuminate:{wavelength_nm, duration_s}, station, rxn}`;
  consumed by the zone compiler in `docs/backlog/ewod-synthesis-protocol.md`.
- Dogfood: rectangle from `se-nucleic-acid` + 3 stub-helix footholds + biped
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
  a free leg (`null`) raises no `chain.dangling_domain`.
- `relax_chain(state=)` for each station stores the walker's per-state pose;
  `get(args={'state': …})` returns poses that differ between stations without
  re-running relax (spy on the name the handler binds,
  `precis_se.chain.relax.relax_bundle`, zero calls on read); a re-run of
  `declare_stations` leaves the stored poses intact; an unrelaxed state
  reports `chain.state_unrelaxed`.
- `view='sweep'` over 3 states runs `envelope_overlaps` and `chain.clash`
  3× each (spy) and reports no clash; a deliberately short leg fails the far
  station only, as `chain.loop_short`.
- Transitions `st0→st1` (`light`, `405nm`) and `st1→st2` (`light`, `365nm`)
  plus reverse edges render in `view='block'`; a transition whose
  `params.guard` is violated by its from-state → `chain.transition_guard`.
- `make_steps` emits 2 ordered steps with wavelengths in `meta`, linked to
  the design.
- Spectral: 6 channels used with `channels_available: 4` →
  `chain.channel_budget` error quoting 6 > 4; two bands with λ_max 20 nm
  apart and FWHM 40 nm → `chain.spectral_crosstalk`.
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
- 2026-09-27 spectral budget lives here, not in `se-nucleic-acid`. Decided.
- OPEN: whether a state may also swap the walker's envelope (bound vs free
  foot) — `BlockState.envelope` already allows it; decide at dogfood.

---
id: precis-se-flow-help
family: se
title: precis — thin-gap flow chambers on an se design (flow: pocket, species BCs, wall_rate)
summary: declare a thin-chamber flow pocket on a block with a 'flow:' shape token (in=/out= patch selectors, dv= vertical dispersion, optional profile/zplane/fluid/species); declare species boundary conditions as patch measures (inlet-velocity or pressure-drop, species-diffusivity, inlet-concentration, wall-rate-constant) scoped '<species>/<prop>'; read computed measures outlet-conversion, outlet-concentration, wall-flux-fraction, damkohler; bind a wall rate constant from a trusted catpath TOF with the wall_rate op
answers:
  - how do I declare a flow chamber on an se block?
  - how do I set an inlet velocity, pressure drop or species diffusivity?
  - how do I make a floor or lid patch reactive, per species?
  - how do I compute outlet conversion or per-patch wall selectivity?
  - how do I turn a catpath TOF into a wall-rate-constant measure?
  - why does my species measure return a reason instead of a number?
applies-to: get/edit (kind='se'); the flow pocket and species measures; wall_rate needs a trusted catpath solve serving a quest
status: active
tags: design
kinds: se, material, quest
---

# precis-se-flow-help — thin-gap flow chambers and species transport

A **flow chamber** is a block with a thin flat void (the gap axis is far
smaller than the two lateral axes) and a pocket whose shape is a `flow:`
token. The kernel solves pressure-driven flow and species transport in
reduced units — the regime card decides which solver a case is valid
for, and every missing input is a **reason string, never a guessed
number**.

## Declare the chamber

```python
edit(kind='se', id='reactor', ops=[{
    'op': 'add_pocket', 'block': 'cell', 'name': 'flow',
    'shape': ('flow:in=patch:cell.side3@0,0+0.006x0.001;'
              'out=patch:cell.side1@0,0+0.006x0.001;'
              'dv=6;species=co'),
    'regions': [{'selector': 'patch:cell.side1@0,0+0.006x0.001'},
                {'selector': 'patch:cell.bottom@0,0+0.09x0.028'}]}])
```

Token keys (all `;`-separated, `in`/`out`/`dv` required):

- `in=`/`out=` — `patch:` selectors naming the port faces (lateral
  faces of the chamber block or of a port plate).
- `dv=` — the reduced vertical dispersion over deepened floor. It is
  required: the model never defaults it.
- `profile=plug|parabolic` — horizontal velocity profile (default
  `parabolic` at this layer).
- `zplane=` — tracer tracking plane, a fraction of the gap (default
  0.99).
- `fluid=<slug>` — a `kind='material'` row the fluid's properties are
  read from: `viscosity` (Pa·s), `diffusivity` (m²/s) so far.
- `species=<name>[,<name>…]` — the transported species names. Species
  scope on measures is the name prefix `<species>/<prop>`.

## Boundary conditions are patch measures

Each BC is an `add_measure` on the chamber block whose `datum` is a
`patch:` region — the region properties mechanism carries them (see
`precis-se-regions-help`):

- `inlet-velocity` (m/s) or `pressure-drop` (Pa) — the operating point
  (pressure-drop needs the fluid's `viscosity`: u0 = h0²·Δp/(12·μ·L)).
- `species-diffusivity` (m²/s) — molecular D; falls back to the fluid
  material's `diffusivity` when no measure states it.
- `inlet-concentration` (mol/m³) — informational while the equations
  are linear.
- `wall-rate-constant` (m/s) — a reactive floor or lid patch; one
  measure per patch, each scored separately. On a lateral patch it is
  refused — there is no wall to bind to.

A bare `wall-rate-constant` applies to every declared species; prefix
the name `<species>/wall-rate-constant` to scope it. With several
declared species a species output measure must be scoped the same way
(`co/outlet-conversion`). The separator is `/`: a `.` in a measure name
is refused (`'block.measure'` relation sources split on the last dot).

## Computed measures (COMPUTERS)

Declare them as ordinary measures with the measurand and a band; DRC
compares the computed value to the band (`measure_unmet` when outside,
`measure_uncomputable` with the reason when the solve cannot run):

- `outlet-conversion` — 1 − outlet/inlet species flux.
- `outlet-concentration` — flux-weighted mean c/c_in at the outlet.
- `wall-flux-fraction` — share of inlet flux consumed on the measure's
  own patch (per-catalyst-patch selectivity). A patch shorter than the
  mass-transfer entrance length with a non-negligible rate carries a
  reason naming the entrance-region bias.
- `damkohler` — the transit Damköhler Da_I (gauge).

The adapter picks the solver by the card: Pe·ε ≤ 0.1 runs the
depth-averaged Eulerian solve; above it the Lagrangian tracer answers
instead — the route is a verdict, not a failure.

## wall_rate — a trusted catpath TOF → k_s

```python
edit(kind='se', id='reactor', ops=[{
    'op': 'wall_rate', 'block': 'cell',
    'datum': 'patch:cell.bottom@0,0+0.09x0.028',
    'source': 'quest:<quest_id>', 'species': 'co', 'roughness': 1.0}])
```

Finds the trusted catpath solve among the refs that `serves` the quest
(trusted = the record carries `log_tof`), and writes
`<species>/wall-rate-constant` on the patch with `origin='proposed'`:
k_s = TOF/(A_site·N_A·c_wall), A_site = `site_area_A2`·1e-20 m²,
c_wall = p/(R·T) from the solve's stated `p_bar` and `T_K` (gas phase
only — liquid-phase sources are refused). `log_tof_p5`/`log_tof_p95`
become the measure's band. The binding probes the apparent reaction
order α at p(1 ± 0.1) when the record can re-solve; |α − 1| > 0.2 means
first-order k_s is valid only while conversion stays below ~0.2 — the
regime card's `apparent_order` line says so.

Refusals all carry a reason: no trusted solve, `tof ≤ 0`, missing site
area, T or p, a liquid phase.

## The regime card

`view='flow'` prints it when the physical inputs are given
(`mu`, `rho`, `u0` or `dp`, plus `molecular_d`, `k_s`, …): thinness,
reynolds, stokes, gravity, peclet, knudsen, then the species lines —
`taylor` (Pe·ε ≤ 0.1 → Eulerian, else the Lagrangian), `wall_damkohler`
(k_s/k_m: reaction-limited / mixed / transport-limited), `damkohler`
(informational), `apparent_order`. A line whose inputs were never given
reads not-applicable and does not block the card.

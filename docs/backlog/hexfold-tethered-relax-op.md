---
status: draft
title: hexfold_tethered generator — relax any .hx net under an authored tether list, no deploy per scene
pillar: 3d-design
prio: high
model: opus
---

# hexfold_tethered generator — relax any `.hx` net under a tether list

Slice 0 of the scene grammar (Reto, 2026-10-07: ruled YES on growing the
`hexfold_scene` grammar from primitives, slice 0 first). Split out of
[hexfold-scene-grammar-from-primitives](hexfold-scene-grammar-from-primitives.md)
on the readiness vet's finding that it does not need the grammar: it is
one generator that turns a "next variant" chat message into an op the
same day. Owner thread: [hexfold-toolkit](threads/hexfold-toolkit.md).

## Motivation / why

- Every scene so far (Y, sp3 fin, 120° seam tube) cost a module and a
  deploy before Reto could look at it. The relax they all run is the same
  call, `stick_relax_pinned`, differing only in which atoms are tethered to
  which surface and which are pinned.
- `.hx` text already describes nets (sheets, tubes, bond attachments,
  `face=`), and the stored-target machinery already judges a relaxed
  block. What is missing is the op that joins the two: "this net, these
  tethers, relax, judge".

## In scope

**Generator** `hexfold_tethered` in `src/precis_se/atomic/generators/`,
registered in `GENERATORS`, reached through the existing `generate` op
(`put(kind='se', id=…, args={'ops':[{"op":"generate","generator":
"hexfold_tethered","name":…,"params":{…}}]})`). No new verb or kind.

**Params**

```
spec:      <.hx text>                      required
tethers:   [tether, …]                     required, ≥ 1
k_tether:  float > 0                       default 1.0 (per-tether override allowed)
terminate: H | ports-open | none           default H (the shipped family rule)
```

A tether:

```
{select: {instance: "s"} | {region: "seam"} | {atoms: [ordinals]},
 surface: plane | cylinder | sphere | line | none,
 params:  plane {point, normal} · cylinder {axis_point, axis, radius}
          · sphere {centre, radius} · line {point, direction},
 k:       float ≥ 0 (optional, overrides k_tether; 0 = free),
 pinned:  bool (optional; pinned atoms do not move and need no surface)}
```

- `select` resolves against the built net: `instance` = a `.hx` instance
  name (every atom placed by that instance), `region` = a `Net.regions`
  name (how the analytic Y, fin and seam-tube nets label seam / wall /
  strip atoms; `.hx` builds expose their instances as regions too),
  `atoms` = explicit ordinals. Unknown names refuse before the relax with
  the available names in the message.
- Every atom must be covered by exactly one tether or `pinned`; an
  uncovered or doubly-covered atom refuses (`tether.coverage`), so a free
  atom is an authored `k: 0`, never an omission.
- Each tether's foot function is the one the adapters use today
  (`y_junction` plane, `fin` cylinder / plane); `sphere` and `line` are
  new, `line` is for seam lines held in register without pinning.

**Relax and judgement.** `stick_relax_pinned(pos, bonds, brest, springs,
sigma, movable, tether, k_tether)` with the per-atom tether composed from
the list; `geometry_findings(net, relaxed=Relaxed(pos, force,
"tethered"))`; `surface.target.unavailable` always (the composed target
is not a surface of revolution); `_block_from_net(..., terminate=…)` with
`extra_topology={"scene": params, "plan": {"relax": "tethered",
"tethers": [resolved counts per tether], "pinned_atoms": n}}`.

**Shared planner, extracted first.** `build_y_junction`, `build_fin` and
`build_fin120` are the same flow (seed → tether → pinned set →
`stick_relax_pinned` → nonfinite guard → `geometry_findings` →
seam/graft findings → `_block_from_net`). Step 1 of this item extracts
it as `relax_tethered(net, seed, *, tether, pinned, k_tether,
ring_sizes, findings) -> RelaxedScene(pos, force, findings)` in
`src/precis_se/atomic/generators/_tethered.py`, re-pointing the three
adapters at it as a pure refactor (their tests pin byte-identical
output). The new generator is then that function behind a param
normaliser.

## Explicitly NOT in scope

- Primitives, composition rules, parameter-set forms of the existing
  scenes: the grammar item.
- Building the Y or the seam tube from `.hx` text. `compose_k3` nets have
  no `.hx` form; parity with Y-A is shown by feeding the Y adapter's
  analytic net and regions through `relax_tethered` (step 1), not through
  the generator.
- A new relaxer, stability or printability claims.

## Acceptance criteria

- Step 1: after the extraction, `tests/test_se_y_junction.py`,
  `tests/test_se_fin.py`, `tests/test_se_fin120.py` and
  `tests/test_se_hexfold_scene_generator.py` pass unchanged (coordinates,
  findings and plan records byte-identical).
- A sheet(6,4) `.hx` spec with one plane tether, `terminate:"none"`,
  returns the same coordinates as `hexfold` fidelity `stick` on the same
  spec within 1e-9 Å (a plane tether on a flat sheet is a no-op).
- A tube(6,6,len=4) with a cylinder tether of the lattice radius and both
  rims `pinned` relaxes with `geom.clash` empty and every atom within
  0.05 Å of the cylinder.
- A sheet with a bud (`bond` attachment, spec §11.1) under two plane
  tethers at 90° (sheet plane, bud plane) returns host angles within
  [100°, 120°] and the bud atoms within 0.1 Å of their plane.
- Coverage refusals: an uncovered atom, a doubled atom, an unknown
  `instance`/`region` name each refuse with `GeneratorError` naming the
  atoms or the available names, and nothing is persisted (the fin120
  misfit test pattern).
- After one deploy, a scene Reto describes in `.hx` plus tethers is
  askable through the session MCP with no further deploy; recorded on the
  thread with the first such op.

## Target + blast radius

- New `src/precis_se/atomic/generators/_tethered.py` (`relax_tethered`),
  new `hexfold_tethered.py` (generator + normaliser), `__init__.py`
  registry.
- `y_junction.py`, `fin.py`, `fin120.py`: call `relax_tethered` (pure
  refactor).
- `src/hexfold/build.py` / `Net.regions`: `.hx` builds expose instance
  names as regions if they do not already.
- Skills `precis-hexfold-help`, `precis-se-atomic-help` (one entry each).
- Tests: `tests/test_se_hexfold_tethered.py` (new); the four adapter test
  files stay green.

## Open questions / decisions log

- 2026-10-07, Reto: ruled YES, slice 0 first (via chat-interface).
- 2026-10-07, ready vet of the parent item: slice 0 is independently
  shippable; split here. Tether addressing by instance name alone cannot
  reach a join's seam atoms; resolved above by accepting `region` and
  `atoms` selectors too.
- Open: whether `line` tethers (seam register without pinning) are wanted
  in the first cut or can wait for the first scene that needs one.

test: tests/test_se_hexfold_tethered.py; tests/test_se_y_junction.py,
tests/test_se_fin.py, tests/test_se_fin120.py (unchanged after step 1).

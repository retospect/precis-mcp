---
id: precis-workspace-optimize-help
family: se
title: precis — evaluate physics on a design you built in a workspace script
summary: precis_geom.physics.evaluate(shape, physics, spec, seed=, cells=) — the one physics call a workspace script makes; flow (thin-gap Hele-Shaw + particle tracer) is its first physics. What it returns, what it refuses, the regime card, the spec shape (hard/soft/gauge), and the rule that the server re-checks every stated number on import.
answers:
  - how do I run the flow kernel on a chamber I built in a workspace script?
  - how do I run a workspace script locally / build atomic geometry with hexfold?
  - what does evaluate return, and why is there no hole count in it?
  - how do I make a flow measure a hard constraint?
  - why does evaluate refuse my cells= value?
  - will the server trust the measures my script reports?
applies-to: workspace scripts that build se/cad geometry and score it with physics
status: active
tags: [design]
kinds: [se, cad]
---

# precis-workspace-optimize-help — physics on a script-built design

A workspace script builds geometry with `precis_geom` (`pg`) and scores
it with **one** call, `precis_geom.physics.evaluate`. It runs inside the
sandbox with no DB and no se. The server's `COMPUTERS` call the same
function on import, so the number the script sees and the number the
design stores come from the same code.

This skill covers `evaluate`. Optimisation loops (params, search,
caching, adapters) and the solid-physics names (heat, current, elastic)
are added by later slices.

Building the geometry itself — the local dev loop (`main.py` →
`precis_geom`/`hexfold` → `out.run` → `scratch/out/<name>/`) — is the
runnable recipe in `packaging/geom/README.md`, not duplicated here: the
PCB sections cover `Board`/fits/trace and "Build an atomic structure
(hexfold)" covers `hexfold.prism.prism_tube` → `pga.geom` → `o.block`.

## evaluate — the call

```python
import precis_geom.physics as pgp
from precis_geom.flow import PortPatch

o = pgp.evaluate(
    (design, expr),          # or a Placed envelope; metres, its own frame
    physics=["flow"],
    spec={
        "inlet": PortPatch(...), "outlet": PortPatch(...),
        "dv": 6.0,                       # D_v* — never defaulted
        "profile": "plug",               # or "parabolic" (default)
        "evenness_region": PortPatch(...),   # a floor/lid rectangle
        "measures": [
            {"name": "rr", "measurand": "resistance-ratio",
             "strength": "hard", "max_value": 1.2},
            {"measurand": "breakthrough-fraction", "strength": "soft"},
        ],
    },
    seed=0,                  # common random numbers
    cells=200,               # the kernel's nx (lateral cells along x*)
)
o.measures["breakthrough-fraction"], o.hard_ok, o.failures, o.cards["flow"]
```

`shape` is the cad solid the script built: a `(Design, Expr)` pair or a
`Placed` primitive. Ports are `PortPatch` rectangles in the solid's
frame (`point`, `normal`, `u`, `v`, `half_u`, `half_v`, metres), passed
either as objects or as their `to_dict()` form. The `selector` field
keeps the `patch:` text the rectangle stands for.

## What comes back — Outcome

| field | meaning |
|---|---|
| `measures` | measurand slug → value. Physics outcomes only |
| `reasons` | measurand → why it has no value (for example, no `evenness_region`). A missing value is never reported as 0 |
| `cards` | physics → regime card, or `None` when the spec gave no `fluid` |
| `hard_ok`, `failures` | the spec's hard measures: `(name, measurand, value, min_value, max_value, reason)` |
| `cost` | `cells`, `nx`, `ny`, `kernel`, `seconds` |
| `paths`, `gap` | the traced swarm and the built `GapDomain`, for a closer look. Never serialised |

`o.to_dict()` is the JSON form, without `paths` or `gap`. Two outcomes
are equal when their `to_dict()` forms are equal. `seconds` is excluded.

**Flow measurands** (`physics=['flow']`):

- `breakthrough-fraction` — share of particles that cross the tracking plane.
- `residence-time-t0` — mean `t*` over resolved particles.
- `resistance-ratio` — `R*` against the same footprint with a flat floor.
- `evenness` — `1 − nonuniformity` of first crossings over `evenness_region`.

**Physics only.** `measures` never contains a feature count, mass,
price, material or process term. Ruling (Reto 2026-10-22): "hole count
will not be the answer to evaluate shape. That's what the tool makes the
space to be." Put those terms into your own objective. You have the
geometry:

```python
cost = -w_b * o.measures["breakthrough-fraction"] + w_n * len(holes) / n_ref
```

Annotate what set the weights: a paper/finding-derived `w_b` or `n_ref`
carries `# <handle> (<why>)` on its line, same convention as the rest of
workspace code ([[precis-workspace-help]] §Cite the DB in the script,
gr514049).

**Common random numbers.** `seed=` reseeds the tracer identically on
every call. Use one seed for the whole search, so two candidate shapes
differ by geometry and not by tracer noise. The same `(shape, spec,
seed, cells)` gives a bitwise-equal outcome. `evaluate` never mutates
`spec` or the design.

## Refusals

Each refusal is a `BadInput` that carries `next=` or `options=`.

- Unknown physics → `options` lists the known names (`flow`).
- A measure whose measurand is not an outcome of the requested physics,
  for example `hole-count` → `next` says design-space terms belong in
  the script's objective.
- Missing `inlet`, `outlet` or `dv`, an unknown spec key, or a `hard`
  measure without `min_value` or `max_value`.
- **Budget.** Above `INLINE_CELLS` = 300 000 lateral cells (`nx × ny`)
  the call refuses before any work, and `next` names `budget='job'`.
  The job lane goes live with the workspace run loop. Until then,
  `budget='job'` refuses too and tells you to lower `cells=`.
- **Shape.** A solid with no unique thin axis (both lateral extents must
  be ≥ 2× the gap) is not a Hele-Shaw gap. 3-D Stokes is out of scope.
- **Regime.** A `fluid` whose Knudsen number is above the continuum
  bound refuses: free-molecular flow runs the nano propagator on
  structure lumens (`view='flow'` on a structure-bound block).

## The regime card

Pass `spec["fluid"] = {"mu": Pa·s, "rho": kg/m³, "u0": m/s}` (or `"dp"`
in Pa instead of `u0`). Optional keys: `particle_d`, `rho_p`,
`molecular_d`, `mean_free_path`. `cards["flow"]` then holds the
six-line card for this geometry's real `L` and `h0`:

- thinness `h0/L ≤ 0.05`
- reduced Reynolds `Re·h0/L ≤ 0.1`
- Stokes number `≤ 0.1`
- gravity ratio `≤ 0.1`
- Péclet `≥ 10`
- Knudsen `≤ 0.01`

A line the inputs cannot evaluate is not applicable, and it never
blocks `ok`. A failing continuum line shows on the card and does not
refuse the call. Read `card.ok` and treat the answer as outside the
model when it is false. The reduced solve itself is dimension-free:
one solve per shape serves every size with the same aspect ratios.

## The spec — hard, soft, gauge

`spec["measures"]` uses the same fields as an se measure: `measurand`,
`strength`, `min_value`/`max_value`, `name`, `datum`, `origin`.

- `hard` — a gate. `hard_ok` is false when the value is outside the band
  or cannot be computed. On import a hard bound becomes a DRC check.
- `soft` — a term of your objective. `evaluate` reports the value and
  you weigh it.
- `gauge` — reported only.

Reject a candidate on `hard_ok == False` before you read its cost.

## Import re-check — the server recomputes

The server does not trust stated numbers. When a run is imported, the
`flow:` pocket and its port measures are stored and `COMPUTERS` run
again: the same `evaluate`, `seed=0`, at the stated `cells`. Two cases
are flagged:

- A stated value that differs from the server's own value by more than
  1e-9 is `measure.script_mismatch`.
- A hard bound that the server's own number violates is a DRC finding,
  whatever the manifest says.

So report what `evaluate` returned, at the `cells` you used.

**Not live yet.** The manifest entry (`out.physics`), the import and
the Flow tab player (`/se/<slug>/flow`) for script-built designs arrive
with the workspace run loop (structure-workspaces slice 1). Today
`evaluate` is library code in the `precis-geom` wheel. The server-side
`FLOW_COMPUTERS` already run on op-built `flow:` pockets through the
same function.

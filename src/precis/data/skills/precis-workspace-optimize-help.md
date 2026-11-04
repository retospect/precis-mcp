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

`evaluate` refuses `spec` values up front. `out.physics` and the import
refuse a few more (see the import re-check below).

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

The server does not trust stated numbers. Declare the run with
`out.physics`:

```python
o.block("c", dsl)
o.physics("c", ["flow"], spec, outcome=o_eval, seed=7, cells=200)
```

The import writes the block's `flow:` pocket and its measures (origin
`user`), then repeats the same `evaluate` once per row, at the `seed`,
`cells`, `n`, `seed_z`, `dv` and `zplane` you declared, on the cad solid
you wrote. The server's number is what the measure stores. The import
still lands when it disagrees, and it names what it found:

- `measure.script_mismatch` — a stated value differs from the server's
  by more than 1e-9.
- `measure.resolution_differs` — the outcome was solved at another `nx`
  or `seed` than the row declares. The value check still runs, so the
  two findings can arrive together.
- `measure.uncomputable` — the server has no number, stated or not. The
  finding carries the reason.

A hard bound the server's own number violates is a DRC finding, whatever
the manifest says. So report what `evaluate` returned, with the `seed`
and `cells` you used.

**What the row refuses.** Each refusal carries `next=` or `options=` and
comes before anything is written.

- More than 8 rows in one run, more than 16 measures on one row, or two
  rows on one block. Each row costs a server solve.
- Rows that together ask for too much solving: `cells² + 30·n`, summed
  over the rows, above 2 500 000 (about a minute). Three rows at
  `cells=860`, `n=2000` fit, and four do not. Search at your own
  resolution and declare the final candidate at one that fits.
- A row on an atomic block. Flow solves a cad solid.
- A wrongly typed payload: a string `dv`, `seed_z` that is not two
  fractions, an `outcome` whose `measures` or `cost.nx` has the wrong
  type.
- A measure with a bad `datum`, a `.` in its name, a non-numeric or
  inverted band, a `hard` strength with no band, or a measurand that is
  not a flow outcome.
- An `evenness` measure with no `spec["evenness_region"]`, or with a
  `datum` that is not that region. It is stored on the region `evaluate`
  scored.
- `spec["fluid"]`. The import stores no SI fluid, so drop the key from
  the spec you pass to `out.physics`; it changes no measure. The regime
  card stays in your script, and the Flow tab takes `mu`, `rho` and
  `u0` or `dp` as query args.

**Ports.** `out.physics` writes a `PortPatch` as a `rect:` selector: the
rectangle itself, in the block's frame. A macro block's render envelope
is a sphere with no named faces, so a `patch:` on the block has nothing
to sit on.

**Re-running.** The manifest is the workspace's whole state. An import
with no `out.physics` drops the pocket and measures the last import
made. It tells your edits from its own by two fields: a pocket whose
shape changed and a measure whose datum changed stay. Any other edit to
them, such as a new band, is dropped with them. If a row needs a name a
kept pocket or measure holds, the import refuses and says so.

**Playing it.** The imported design opens in the Flow tab at
`/se/<slug>/flow`. Until the workspace run loop lands (structure-workspaces
slice 1) you run the script yourself and import the tree with the
`import_workspace` op ([[precis-workspace-help]]). `budget='job'` waits
on the same loop.

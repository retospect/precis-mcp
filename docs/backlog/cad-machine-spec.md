---
status: draft
title: cad — from part modeler to machine spec (instancing, ports, joints, motion)
model: opus
---

# cad — from part modeler to machine spec

## Motivation / why

The `cad` kind is a strong **part** modeler: a boolean DAG of placed analytic
primitives you probe instead of render. Almost nothing in it knows what a
*machine* is. Two structural facts set the agenda:

- `scene.py::parse_source` has exactly two directives (`component`,
  `desc:`/`use:`). **No design can reference another design.** Every design is
  a flat leaf, so there are no reusable assemblies — only atoms.
- `relate.py::translational_dof` *measures* freedom from geometry. Nothing
  *declares* it. A machine is defined by its declared motion; measured DOF is
  a consequence of geometry, not a statement of intent.

A rough machine spec needs to say: *these parts, mated at these interfaces,
move like this, made of this, bought from here* — and then be asked the
question that matters: **does anything collide anywhere in its travel?**

Design session 2026-09-04 (Reto + agent). Ordering below is the build order:
each slice is independently useful, and slice 4 is the one that makes the
kind worth reaching for.

## Slice 1 — instancing (`use <slug> as <name>`) — **SHIPPED**

Landed as designed below: `scene.expand_instances` + the injected
`Resolver`, `precis.cad_resolve.design_resolver` as the one store-backed
implementation, and expansion wired into the handler (put / derive / probes /
exports), the web routes (glTF, scene.json, analysis, export) and both
`cad_*` job types. No migration — an instance is a `NodeSpec` with
`config="use:<slug>"`. Tests: `tests/test_cad_instance.py` (kernel),
`tests/test_cad_handler.py` (DB round-trip), `tests/precis_web/test_cad.py`
(scene.json). Two guards beyond the plan: a design may not instance its own
slug (the resolver would hand back its *previous* save), and the web
analysis cache is bypassed for instancing designs (a sub-design re-put moves
geometry the parent's `updated_at` can't see).

Left open from this slice: **decision 4** below (a `contains` link
design→design for the assembly tree in `view='links'`) — it needs a new
`relations` row, so it wants its own migration rather than riding along here.

The one true "molecule" verb; everything else is worth less until it exists.

Source directive, top-level (peer of `component`, not a node inside one):

```
use flange as f1 @0,0,40 rot:0,0,90
use bolt   as b  @18,0,0 polar:n6r18
```

**Implementation: expand at the `SceneSpec` level, not in `build_design`.**
An `expand_instances(spec, resolve) -> SceneSpec` inlines the referenced
design's nodes under the composed transform, namespacing names and components
(`f1.plate`, `f1.hub_bore`). Every downstream consumer — probe, relate,
connectivity, `to_openscad`, tessellate, STL/3MF/STEP — then works unchanged,
because they all consume a flat `SceneSpec`/`Design`. Expanding inside
`build_design` would fix the probes and silently break every export.

- The **stored** spec keeps the compact `use:` node (one row, one chunk); the
  expansion is ephemeral. The node tree shows the `use:` line; probes see the
  inlined bodies.
- Persistence needs **no migration**: an instance is a `NodeSpec` with
  `config="use:<slug>"`, `op="add"`, `component=<instance name>`.
- Sub-design patterns explode into discrete nodes during inlining — a `polar:`
  array is defined around the *node's own* z axis at its `@x,y`, which is not
  expressible as a `NodeSpec` pattern once an arbitrary instance rotation sits
  above it. `_pattern_transforms` already yields the per-copy transforms;
  `vec.euler_deg_from_matrix` decomposes each composed transform back to the
  `loc`/`rot` a `NodeSpec` carries.
- `precis.cad` imports nothing from the DB and must stay that way, so the
  slug→`SceneSpec` **resolver is injected** (`resolve` callable, default
  `None`). A spec containing instances with no resolver is a `SceneError`, not
  a crash. The handler's resolver is `resolve_live_slug_ref` + `cad_load`.
- Guards: cycle detection over the slug stack (`a → b → a`), a depth cap, and
  a total-node budget so a 6× nested 6-polar design can't detonate the worker.
- Fast path: a spec with no instance nodes returns unchanged, so existing
  designs are byte-identical through the new code.

**Acceptance:** a design that `use`s two others probes, exports, and renders
as a real multi-body assembly; `connectivity` reports contacts across the
instance boundary; a cycle is a clean `BadInput`; the no-instance path is
unchanged.

## Slice 2 — ports + mates

`port drive @0,0,12 rot:0,0,0` declares a named interface on a component;
`mate motor.shaft to gearbox.drive` places one instance by making its port
coincide with another's (with the usual flip convention: mating faces oppose).

Machines are assembled by mating named interfaces, not by typing world
coordinates. This is also the largest **LLM authoring** win available: today
the model must do trig to place a bolt circle on a rotated face, and it
silently drifts. The skill's "use `calc` for bolt-circle coordinates" tip is a
symptom of this missing feature, not a workaround for it.

Solve mates by direct substitution (each mate fully determines one instance's
pose from an already-placed one) — a spanning tree over the mate graph, not a
general constraint solver. Over-constrained or cyclic mate graphs are a
`SceneError` naming the cycle. A general iterative solver is out of scope.

**Acceptance:** a two-part assembly authored with zero world coordinates
builds to the same geometry as the hand-placed version; an unreachable
(unmated, unplaced) instance is reported at `put`.

## Slice 3 — joints

`joint arm revolute axis:0,0,1 at:port shoulder limits:-90..90`, plus
`prismatic`, `cylindrical`, `screw` (pitch), `fixed`, and ratio pairs
(`gear`, `belt`) so a driven chain propagates. A design gains a **state
vector**; `get(..., args={'state': {...}})` poses it.

Joints are declarations layered on the mate graph — they do not change the
static geometry, they parameterize it.

**Acceptance:** a posed design probes correctly at any legal state; an
out-of-limit state is rejected; joint-free designs behave exactly as today.

## Slice 4 — `view='sweep'` (motion interference)

Pose the joint chain across its declared limits; report collisions (which two
bodies, at which state) and the swept envelope per moving body.

*"Does the arm hit the frame anywhere in its travel?"* is the question a
machine spec exists to answer, and today it is unaskable. This is the payoff
for slices 2–3 and the point of the whole sequence.

Sampling strategy: coarse sweep over the state space, then bisect toward first
contact on any pair whose clearance goes negative. Report the *state*, not
just a boolean — a collision you can't locate is not actionable.

**Acceptance:** a deliberately-colliding mechanism reports the offending pair
and the state at first contact; a clear mechanism reports its minimum
clearance over travel and where it occurs.

## Parallel track (independent of 1–4)

- **`material: <slug>` per component → `view='mass'`.** The `material` kind
  already stores sourced density; wiring it in yields mass, CoM and an inertia
  tensor that arrive **cited**, which no other CAD tool does. `volume` is
  sampled with a ±error — carry that error through to mass, never launder it.
  Then `view='balance'`: is CoM inside the support polygon, what are the
  support reactions, does it tip.
- **Catalog atoms backed by `component`** — `part bolt1 M6x20-hex`,
  `bearing:6202`, `rail:MGN12`, `extrusion:2020`, `nema:17`, `gear:m1z20`.
  These are the literal building blocks of machines and precis is unusually
  placed here: they resolve to **procurable `component` refs with sourced
  specs**. Start with an envelope + mount pattern + ports; nobody needs real
  thread geometry. Feeds `view='bom'` — flatten instances + catalog parts →
  quantities → `component` refs → cost/lead time. `component` already has a
  `bom` view with a `spec=` consistency query; feed it, don't grow a second one.
- **`thread:M6x1` as an annotation**, not geometry — the declaration is what
  engagement checks, fastener BOM and "export as a plain hole" need. Never the
  helix.

## Tier-2 probes (after slice 4)

- **`view='fits'`** — sweep all part pairs, classify each gap against ISO 286
  (clearance / transition / interference), plus a tolerance stack along a named
  chain. The skill currently says "whether −0.02 mm is intended is your call";
  it doesn't have to.
- **`view='fasten'`** — per fastener: grip length, thread engagement,
  bottoming out, and driver/wrench access (a cone probe from the head). The
  most common real assembly error, and it is pure geometry the kernel has.
- **`view='envelope'`** — per-part and whole-machine bbox/hull against a
  declared build volume, enclosure or shipping crate.
- **`view='diff'`** — structural + geometric diff between two designs or
  revisions. The propose/apply loop derives a new slug with no way to see what
  the model changed; this closes it alongside `cad-diagnose-apply-loop.md`.
- **Manufacturability** — `draft` exists (mold pull); add minimum wall
  thickness, FDM overhang angle, mill tool-reach.

## Profiles + revolve/extrude/sweep (expressive gap, unslotted)

The frustum family cannot express a bent bracket, a tube elbow, an O-ring
groove, a cam, or a belt path. `profile:` (polyline + arc chain, in-plane)
with `revolve:θ` / `extrude:h` / `sweep:path`. A revolve of a polyline stays
closed-form for membership and ray, so this does **not** cost the analytic
property the whole kind rests on. Sequence it when a real design needs it.

## `check:` assertions (sequence with slice 1 or later)

`check: clearance(shaft,hub) > 0.05`, `check: mass < 2kg`, `check: one_solid`.
`put` already warns on floating bodies and interference — generalize that into
declared, re-runnable requirements. This is what makes LLM-edited designs
*safe*: a `cad_propose` apply can be gated on the design's own checks, and a
recurring watch can re-verify. It is also the honest form of "rough spec" —
you state intent and the kernel keeps it true.

## Explicitly NOT in scope

- **Fillets / rounds** — cosmetic at rough-spec altitude.
- **True thread geometry, splines, sheet-metal flat patterns.**
- **FEA and dynamics.** If load answers are ever needed, export STEP and rent
  a kernel; do not grow one inside an analytic IR.
- **A general constraint solver** for mates (slice 2 is substitution over a
  spanning tree).
- Any change that makes `precis.cad` import from the DB or the store.

## Target + blast radius

- `src/precis/cad/scene.py` — parser directives, `SceneSpec`,
  `expand_instances`, `build_design` signature.
- `src/precis/cad/relate.py`, `probe.py`, `bulk.py` — new views.
- `src/precis/handlers/cad.py` — resolver injection, `_VIEWS`, tree render.
- `src/precis/cad/export.py`, `tessellate.py`, `gltf.py` — consume the
  expanded spec.
- `src/precis_web/routes/cad.py`, `templates/cad/` — viewer + editor.
- `src/precis/workers/job_types/cad_propose.py`, `cad_discuss.py` — dry-run
  path must expand before building.
- `src/precis/data/skills/precis-cad-help.md` — the runtime surface.

## Open questions / decisions log

1. Instance namespacing separator — `.` (chosen: reads well in the tree, and
   no existing node name may contain it; enforce that in the parser).
2. Do probes descend into instances by default? (Lean yes, with
   `args={'depth': 1}` to stop at the boundary.)
3. Does `use` of a *retired* design resolve? (Lean: no — a hard error, since a
   silently-empty sub-assembly is the worst possible failure mode.)
4. Should slice 1 also emit `contains` links design→design, so `view='links'`
   shows the assembly tree? (Lean yes, cheap, and the graph machinery exists.)

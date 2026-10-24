---
status: in-progress
claimed-by: fitcheck-s1
title: se: built-in fit / interference / mass check and a probe primitive for scripted designs
pillar: 3d-design
prio: high
---

# se: built-in fit / interference / mass check and a probe primitive for scripted designs

Source: gr514325 (Reto approved the plan and the probe, 2026-10-24: "good,
try that"; v2 rulings on the review, same day). Context gripes: gr514242
(no envelope on macro blocks), gr514245 (no material/measure keys),
gr514241 (template blocks count as assembly), gr514251 (unicycle-c1 design
bugs that only geometry shows).

## Motivation / why

The unicycle dogfood agent rated the se views 3/5 and trusted its result
only because it wrote its own checker
(`/tmp/gripe-fix/fixtures/unicycle/check.py`, 107 lines): compose instance
poses from `manifest.json`, flatten through `precis.cad.scene.build_design`,
`bulk.volume` x a hardcoded density table, bbox through the private
`relate._bounds`, `relate.clearance` for connected and near pairs. It found
spokes 3 mm into the fork, bolts without holes and bolt heads on the wrong
end. The se views could not, because macro (cad DSL) blocks get no envelope
on import (`precis_se/workspace_import.py::_block_envelope` has no call
site for them).

The gripe asks for one check that **every script build runs**, on real cad
geometry, with script lines on both parts, intended contacts from
`out.connect`, mass from the material, and a diff against the previous
build. Reto on speed: "It should take a few milliseconds. It's just
hundreds of things."

## Measured (2026-10-24, unicycle fixture, 19 instances, 13 templates)

Scripts: `/tmp/fitrev_scripts/{measure,mesh,ray}.py` (not in the repo).

| step | analytic (`relate.clearance`) | mesh (manifold3d 3.5.2), chord 0.01 mm | chord 0.1 mm |
|---|---|---|---|
| tessellate + boolean-fold 13 templates | n/a | 56 ms (wheel 31 ms, 3820 tris) | 20 ms (wheel 12 ms) |
| place 19 instances (lazy transform) | n/a | 0.9 ms | 0.3 ms |
| 171 pairs, gap (search 2 mm) | ~450 s (est.) | 31 ms (0.18 ms/pair) | 13 ms |
| 34 AABB-near pairs, gap | 204 s (0.35–2.2 s, wheel pairs 13–18 s) | 28 ms | 12.5 ms |
| intersection volume, 34 near pairs | n/a | 24 ms (0.7 ms/pair) | 11.5 ms |
| one thin ray through a member | `cad.probe.ray_hits` 0.1 ms | `Manifold.ray_cast` 3 µs | 3 µs |

End to end, uncached, all 171 pairs at 0.01 mm: about 110 ms. With
tessellations cached by geometry hash: about 55 ms, and an edit that moves
one part re-runs only that part's pairs, so a few ms. A 100-part design
(4950 pairs; AABB test vectorised, ~300 near pairs) projects to ~0.3 s
uncached and tens of ms incremental.

Agreement on the 34 near pairs (17 connected, 8 wheel near-pairs among
them):

- **Overlap yes/no:** 14 of 14 `relate` interferences have mesh
  intersection volume > 0 at every chord tested. Seated pairs read volume 0,
  except two pairs on a 180-degree-rotated instance (axle/bearing_right,
  axle/crank_right) whose coaxial polygons are out of phase: spurious
  thickness 0.005 mm at chord 0.01, 0.046 mm at 0.1, i.e. at most the chord
  tolerance.
- **Positive gaps:** where both methods are sound, |difference| is at most
  2 chord tolerances (crown/seatpost 0.250 vs 0.240; seatpost/clamp bolt
  3.500 vs 3.512; pedal/wheel 66.194 vs 66.203).
- **`relate` is wrong on two pairs.** axle/fork is 2.5 mm by `relate` and
  13.49 mm by mesh; hand check: axle r10 in a r23.5 bore, so 13.5 mm (the
  CSG SDF `max(d_base, -d_cutter)` is only a distance bound inside carved
  bodies). bearing/wheel is 1.0 mm by `relate` and 0.148 mm by mesh; hand
  check of the 60-degree spoke against the bearing's outer corner gives
  about 0.15–0.2 mm (the 14^3 seed grid over a 508 mm region misses it).
  So `relate.clearance` cannot be the referee as-is (Decision 11).
- **Overlap magnitude is a different measure.** `relate`'s negative gap is
  2 x the deepest half-overlap (a fully buried M5 shank reads -5.0). Mesh
  reports intersection volume plus thickness 2V/A (same shank: 234 mm^3,
  2.06 mm). The report shows volume and thickness and names them; it does
  not imitate `relate`'s number.

BVH: not needed beyond manifold. `min_gap` and the booleans are
BVH-accelerated inside manifold, and the pair loop's AABB test is one numpy
pass. trimesh is not in `uv.lock` and is not needed (`Manifold.ray_cast`
exists in 3.5.2).

## Rulings (Reto 2026-10-24)

v1 rulings, still binding:

- **gr514235:** `view='ops'` takes slicing args; the fold rule stays. Not
  built here.
- **gr514241:** template blocks are not assembly parts (see slice 5 for how,
  without breaking instance inheritance).
- **gr514245:** material and measure come first and are script-owned;
  binding and BOM are design-owned and specced later (out of scope).

v2 rulings on the review:

- **A.** Checks use real cad geometry. Envelopes are for render and broad
  phase only.
- **B.** `precis.fit_classes` and its data JSON (`precis/data/fit_classes.json`)
  join the precis-geom wheel's allow-list.
- **C.** The fit runs live in views whenever it is fast, and at `finish()`
  with caching.
- **D.** Slice order as below (engine and offline check first, envelopes last).
- **E.** Material comes from the cad DSL's existing `material <component>
  <slug>` lines, not a new block key.
- **F.** Prod `unicycle-c1` is a post-deploy check, not a test.

## Decisions

1. **Mesh engine.** `precis/cad/fit.py` (pure, no DB, no `precis_se`
   import) tessellates each component once with
   `precis.cad.tessellate`, folds its booleans in manifold3d (the
   `precis.cad.export::_component_solids` recipe), and places instances by
   lazy `Manifold.transform`. Gap: `Manifold.min_gap(other, search)`.
   Overlap: intersection `volume()` and thickness `2V/surface_area()`.
2. **Chord tolerance.** Per component, `chord = min(0.01 mm, 1e-4 x the
   component's AABB diagonal)`, so small parts get finer meshes and the rule
   stays scale-relative. Circular primitives get
   `n = ceil(pi / acos(1 - chord/r))` segments (`tessellate._circular(seg=)`;
   the export path's fixed `_FN = 64` stays for export). Inscribed polygons
   bias every circular surface inward by at most `chord`. The report states
   the chord it used.
3. **Contact band and referee.** A mesh result within `3 x chord` of a rule
   threshold, or an overlap thinner than `2 x chord`, is "within mesh
   resolution". The referee re-tessellates just those two components at
   `chord/10` and recomputes, which is deterministic and converges. It is not
   `relate.clearance` (Decision 11).
4. **Geometry hash and cache.** The key is sha256 of the template's
   canonical `SceneSpec` text (`precis.cad.dsl.format_spec` per node) plus
   the chord. The pair cache key is (hash a, hash b, relative transform
   rounded to 1 µm, rule). Offline the cache is `out/.fitcache/`; on the
   server it is an in-process LRU (no DB). A rebuild re-runs only pairs whose
   key changed.
5. **Gap rules are keyed by mechanism.** A connect's expected gap comes from
   `joint.mechanism` (`precis_se/joints.py::MECHANISMS`), falling back to
   `joint.class` only when no mechanism is declared:

   | mechanism (else class) | expected | notes |
   |---|---|---|
   | `press` | gap <= 0 | overlap thickness reported; a press with gap > tol is a finding |
   | `bearing` | 0 | seat and bore |
   | `key`, `clamp` | 0 | |
   | `screw` | 0 | overlap allowed up to `(d - d_minor)/2` in the terminal member only (thread engagement) |
   | `snap` | 0 | |
   | `integral`, `bond`, `cable`, `tensioner`, `magnet` | not checked | listed as "not fit-checked" |
   | class `prismatic`, `cylindrical`, `revolute` (no mechanism) | gap > 0 | sliding or turning fit |
   | class `rigid` (no mechanism) | 0 | |

   Tolerance: `tol = max(0.05 mm, solver resolution, per-process value)`.
   Solver resolution is item 3's mesh band. The per-process value comes from
   `precis_se.capabilities` for the block's mode when it has one (printed
   parts: 0.1–0.2 mm; `hole_compensation_m` is the precedent). Override:
   `joint.params.fit = 'press'|'bearing'|'slide'|'fixed'` and/or
   `joint.params.fit_tol` (metres), vetted in `joints._vet_params`, so no new
   connect key. If a pair has several connects, the strictest rule wins and
   both connects are named. The fixture's own conflicts are expected
   findings: crown/seatpost is `prismatic`+`press` at +0.24 mm (press wins:
   finding), and pedal/crank is `revolute`+`bearing` at -0.9 mm thickness
   (bearing: finding).
6. **Fastener size for the bore check.** A scripted fastener is the
   endpoint of a `mechanism='screw'` connect whose port has role `thread`.
   Its size is that port's `annotations.thread` (`'M5x0.8'`; the importer
   already vets it, `workspace_import.py::_vet_port_annotations`) and its
   thread depth is `annotations.depth`. The clearance diameter comes from
   `precis.fit_classes.clearance_hole(size, fit_class)` (ISO 273 medium
   unless `joint.params.fit_class`). Major diameter and pitch come from
   `precis.component_series`. A catalog-bound screw (`fasten._find_fastener`)
   uses its catalog row. If no size can be found, the check refuses and names
   the block, the port and the annotation to add; it never infers a size from
   the shank radius.
7. **Probe depth comes from exact ray spans.** Detection is the swept solid
   (cylinder or cone, as a manifold) intersected with each placed member: any
   volume above the band in item 3 is a hit. Depth, entry and exit come from
   `precis.cad.probe.ray_hits` (analytic interval arithmetic, 0.1 ms per ray,
   names the node that cut a void) on the axis plus a ring of rays at the
   swept radius (8 by default). No bisection.
8. **Previous-build diff.** Offline it is `out/fit.json` (current) and
   `out/fit.prev.json`, rotated beside `trace.json`/`trace.prev.json` by
   `finish()` (the `precis_geom/out.py::_prev_trace` pattern). On the server
   it lives in the design ref's meta at `workspaces[<ws>].fit` (JSON, no
   migration). Findings are keyed by (rule, sorted pair), never by value. A
   mass delta smaller than the summed volume `rel_err` reads "about 0".
9. **Trust boundary.** A fit report written by the script is untrusted
   input. The importer recomputes (or re-verifies by cache key) and stores
   the server's report. The view never shows the script's verdict as
   verified.
10. **Layering.** `precis/cad/fit.py` holds the engine and takes data in: the
    gap-rule table, the size tables, and labels (block name, made-by line)
    supplied by the caller. `precis_geom/fit.py` and `precis_se/fit.py` are
    thin adapters (manifest + `trace.json` frames, or `SeTree` + stored
    trace). `precis_geom` imports only `precis.cad`, `precis.fit_classes`
    (ruling B) and `precis.component_series`, per the import-linter contract
    "precis_geom imports only the precis-geom wheel's carved precis subset"
    in `pyproject.toml`.
11. **Referee is mesh refinement, not `relate.clearance`** (Reto
    2026-10-24, answering Questions 1): mesh refinement at `chord/10` is
    the referee; `relate.clearance` is retired from se checks (gr514448
    tracks it). The fit engine never calls `relate.clearance`.
12. **Field leaves (`rd`, `blend`, `field:`/`relief:`)** (Reto 2026-10-24,
    answering Questions 2): listed as "not fit-checked" in v1 — reported
    as an info row, never a failure.
13. **Default-on at `finish()`** (Reto 2026-10-24, answering Questions 3):
    `finish()` runs the check by default and `check=False` opts out. A run
    whose check fails still writes `manifest.json`; only `strict=True`
    raises.
14. **Multi-connect naming** (fitcheck-s1, slice 1, spec ambiguity): when
    >1 connect lands on a pair, the report's `rule` cell names the winning
    rule plus every connect's `a.port↔b.port` endpoints ("the strictest
    rule wins and both connects are named").
15. **`Placed.refine` shape** (fitcheck-s1, slice 1, spec ambiguity): the
    referee callback is `refine(finer_chord) -> {component: Manifold}`
    returning **already-placed** solids, so the engine recombines and
    re-measures without knowing templates or frames.
16. **`fit()` extra kwargs** (fitcheck-s1, slice 1, spec ambiguity): the
    spec signature lists `*, sizes=None, materials=None`; the engine also
    takes keyword-only `min_clearance` (per-pair overrides — the manifest
    `fit_rules` rows) and `pair_cache` (the Decision-4 pair memo), both
    caller-supplied data, keeping the engine pure.

## Slices

| # | what | Devin alone? | Opus seam review? | migration |
|---|---|---|---|---|
| 1 | mesh engine `precis/cad/fit.py` + `fit_classes` in the wheel + `fit.check(run_dir)` + `out.check()` at `finish()` with cache | yes | yes (engine API, units, determinism) | none |
| 2 | server `view='fit'`: live when fast, stored report from import, diff in ref meta | yes | no | none |
| 3 | mass from DSL `material` lines (ruling E) | yes | no | none |
| 4 | probe primitive, bore check, `out.probe` | no (Opus owns the head-side rule) | yes | none |
| 5 | consumers on real cad (ruling A): validate / clearance / DOF / toolaccess / insertion_path; template handling; render envelopes | no | yes (rewrites shipped checks) | none |

### 1. Mesh engine and offline check

**Claimed:** fitcheck-s1 (Devin, 2026-10-24).

Files:
- `src/precis/cad/fit.py` (new): `tessellate_component(spec, component,
  chord) -> Manifold`, `place(...)`, `fit(placed, connects, rules, *,
  sizes=None, materials=None) -> FitReport`, `FitReport.text()`.
- `src/precis/cad/tessellate.py`: `_circular` already takes `seg=`; add a
  `chord=` path to `mesh_shape` (the JS parity test covers only the default
  path, so leave `_FN` alone).
- `src/precis_geom/fit.py` (new): read `manifest.json`, `blocks/*`,
  `trace.json`; compose world poses (parents included, `linear`/`polar`
  instance arrays expanded); call the engine. Also `python -m
  precis_geom.fit <run_dir>`.
- `src/precis_geom/out.py`: `Run.check(*, strict=False, min_clearance=None,
  materials=None)`. `finish()` runs it by default and writes `out/fit.json`
  (and rotates `fit.prev.json`); `out.run(..., check=False)` opts out.
  `strict=True` raises on any error finding.
- `pyproject.toml` import-linter allow-list plus the wheel's force-include
  list (`packaging/geom/`): add `precis.fit_classes` and
  `precis/data/fit_classes.json` (ruling B);
  `tests/workspaces/test_geom_wheel_imports.py` derives from the same list.

Engine, in order:
1. **Flatten.** Each template is tessellated once (cache, Decision 4). Each
   instance is placed by its world transform. Template-only blocks (blocks
   referenced as some instance's `template`) are not parts.
2. **Pairs.** Vectorised AABB test over the placed bounding boxes, inflated
   by `max(search, tol)`; connected pairs are always kept. Parent/child
   pairs are skipped, as in check.py. Gap is `min_gap` with
   `search = max(2 mm, rule margin, 10 mm on connected pairs)`. Overlap is
   intersection volume and thickness.
3. **Classification** by Decision 5. A non-connected pair with gap < tol
   (touch or overlap) is a finding. Any pair in the contact band gets the
   Decision 3 referee.
4. **Per-pair rule:** `min_clearance={("a", "b"): "2 mm"}`. Values are unit
   strings through `precis.utils.units` (bare numbers are metres); the
   manifest key is `fit_rules`.
5. **Mass** (slice 3 fills it): per block, total, CoG, world bbox.
   Offline, mass needs `materials=` (slug -> kg/m^3), because the material
   kind lives in the DB. Without it the mass line carries a refusal and the
   other checks still run.

Report rows: `severity | pair | gap or overlap (volume, thickness) |
expected | rule | made-by a | made-by b`. Made-by offline comes from
`trace.json` frame ids on the manifest entries. Pairs and blocks are sorted,
and values are rounded to 1 µm, so two runs give byte-identical text.

Tests:
- `tests/test_cad_fit.py`. A synthetic fixture with:
  - a seated pair (clean);
  - a 3 mm overlap (finding, both lines);
  - a connect with a +5 mm gap (finding "expected 0");
  - a press at -0.05 mm (passes);
  - a pair 180-degree rotated about a coaxial bore (clean: phase artefact
    inside the band, the referee confirms);
  - a linear instance array;
  - a two-level parent chain;
  - a part in m next to one authored in mm;
  - determinism (two runs, identical text);
  - an AABB-only stress check of 100 random boxes under 200 ms.
- `tests/workspaces/test_geom_fit_check.py`: `fit.check(run_dir)` on a
  checked-in copy of the unicycle fixture reports axle/wheel, fork/wheel and
  crank/wheel spokes overlap (~3 mm wrong-plane spokes), the four bolt/member
  overlaps (bolts without holes), and crank/pedal; and the whole run takes
  under 300 ms cold.
- `tests/workspaces/test_geom_wheel_imports.py` passes with the new
  allow-list entry.

### 2. Server `view='fit'`

Files: `src/precis_se/fit.py` (adapter: `SeTree` world poses from
`ops.compose_world_pose`, bound cad via `tree.cad_designs` as in
`rays_macro.py::block_geoms`, envelope fallback for blocks with no cad);
`src/precis_se/handler.py` (register `view='fit'`);
`src/precis_se/workspace_import.py` (accept top-level `fit` and `fit_rules`
in `_TOP_KEYS`; recompute on import, Decision 9; store the summary in
`workspaces[<ws>].fit`, Decision 8).

Live when fast (ruling C): the view computes live if the projected cost
(cached tessellations plus changed pairs, at the measured ~0.2 ms/pair) is
under 200 ms. Otherwise it serves the stored report, labelled with its
build and age, and says how to force a recompute. An envelope-only design
(no bound cad) runs on posed envelopes and the report says so.

Diff section: new, cleared and changed findings, plus the mass delta.

Tests: `tests/test_se_fit_view.py`: the slice 1 synthetic fixture
imported; the view text equals `fit.check(run_dir)` apart from the header;
a second import with the overlap removed shows it under "cleared" with a
mass delta; with a new overlap it shows under "new"; an envelope-only
design gets a labelled report.

Post-deploy check (ruling F, not a test): `view='fit'` on prod
`unicycle-c1` reports gr514251's items as connect-rule findings: axle 5 mm
short of both cranks (expected 0); bearing_right in or outside
fork_right; pedal_right not at crank_right's pedal mount (pedals both at
x=+170, ~340 mm off).

### 3. Mass from the DSL `material` lines (ruling E)

The cad DSL already carries `material <component> <slug>`, and cad
`view='mass'` (`precis/handlers/cad.py::CadHandler._render_mass`) already
turns that into sourced density with an error. Lift the math (volume x
density, the error carried through, CoG from `bulk.volume`'s centroid) into
`precis/cad/fit.py` as `mass_properties(design, densities)`, and have
`_render_mass` call it. On the server, densities come from the material
kind. Offline, from `materials=`.

A missing or unknown material refuses mass with one message naming every
offending block and component and the nearest material slugs. There is no
default density. The rest of the report still runs.

`measure` keys stay stored-only in v1 (gr514245); no consumer here.

Tests: `tests/test_cad_fit_mass.py`: a known box with a known density gives
the expected grams within `rel_err`; two components without material give
one refusal listing both; `_render_mass` output is unchanged.

### 4. Probe primitive and bore check

Files: `src/precis/cad/probe.py` (extend, it exists: add `sweep()` and
`SweepHit` beside `ray_hits`/`Hit`; caller-supplied labels, because the cad
layer knows no blocks or lines); `src/precis/cad/fit.py` (bore check);
`src/precis_geom/out.py` (`Run.probe`); `tests/test_cad_probe.py` (extend).

API:

```
sweep(placed, origin, direction, length, diameter, cone_half_angle=None,
      *, ignore=(), cull=None, rays=8) -> list[SweepHit]
SweepHit(label, entry, exit, depth, volume, feature)
```

Detection and depth follow Decision 7. `cull(box) -> bool` generalises
`toolaccess._behind_the_drive_face`. A diameter of 0 is a single thin ray
(`ray_hits`).

Bore check per scripted fastener (Decision 6):
1. Sweep the clearance diameter through every clamped member, from the
   head's bearing face to the terminal member. Any hit is a missing or
   misaligned hole, reported with the fastener's and the member's lines.
2. Sweep the major diameter into the terminal member and expect hits only
   within `annotations.depth`.
3. Head side. Along the port's direction from the port origin, the first
   material reached that is not the fastener must be a clamped member within
   one pitch. In the opposite direction, the head's material must come
   before any member. If it doesn't, report "head on the wrong end".

Script-declared probes: `out.probe(name, origin, direction, length,
diameter, cone_half_angle=None, *, block=None, expect='clear'|'hit'|n)`.
Lengths are unit strings and the frame is the block's (or world when
`block=None`), as for `out.ray`. Probes are reported in the fit view as
pass/fail. `out.ray` keeps its observation role; both share `ray_hits`.

Tests:
- `tests/test_cad_probe.py` (extend): cylinder through a plate with a hole
  (no hit) vs without (hit, depth = plate thickness exactly); a cone widening
  hits what a cylinder misses; `ignore` and `cull`; depth matches the exact
  span within 1e-9 relative.
- `tests/test_cad_fit_bore.py`: the unicycle fixture with
  `annotations.thread` added to the bolt ports flags all five bolts as
  missing holes and the flange bolts' head side; a bolt port without the
  annotation is refused, naming the block.

### 5. Consumers on real cad (ruling A), templates, render envelopes

Files: `src/precis_se/validate.py` (`envelope_overlaps`),
`src/precis_se/toolaccess.py` (`access`, `insertion_path`,
`_insertion_obstacles`), the DOF probe and `view='clearance'` in
`src/precis_se/handler.py`, `src/precis_se/workspace_import.py`.

- A block bound to cad (`bound_kind == 'cad'`, the template's binding for an
  instance) is checked on its placed mesh from slice 1. Its envelope is used
  only for render and the broad phase. toolaccess and insertion_path keep:
  - first-hit semantics;
  - soft material-ancestor hits;
  - stamped hole cutters, subtracted as manifold booleans;
  - the per-screw budget;
  - the `/se/<slug>/tool-volumes.json` render records
    (`precis_web.blocktree_3d.tool_volume_group`).

  They swap the narrow phase (`relate.clearance` -> mesh intersection), not
  the logic.
- Render envelope at import for macro blocks: a sphere about the template
  origin containing its mesh AABB, through
  `precis_se.atomic.generate.ingest_envelope` in canonical metres. A `box`
  envelope can't hold an AABB that doesn't straddle the origin (`box` is
  centred in x/y with its base at z=0 and takes no offset,
  `precis/cad/dsl.py::_ALIAS_KEYS`), and with ruling A the envelope no
  longer drives any check, so a sphere is enough.
- Templates (gr514241): the template keeps its envelope, because instances
  inherit it (`precis_se/ops.py::effective_envelope`). The template node
  itself is excluded from validate, clearance, drc, fit and `view='tree'`
  pose rendering, and listed under "templates". A block counts as a template
  when some instance names it, or when it carries `template: true`
  (`_BLOCK_KEYS` in `workspace_import.py`, `out.block(..., template=True)`)
  for a template with no instance yet.

Tests:
- `tests/test_se_insertion_path.py` and `tests/test_se_toolaccess.py`
  unchanged and green. The refactor is its own commit after slice 4.
- `tests/workspaces/test_import.py`: a macro block gets a render envelope
  containing its AABB; template nodes raise no `block_without_envelope` and
  no overlap.
- validate on the unicycle reports the cad overlaps, not sphere-envelope
  overlaps.
- A macro block whose cad fails to build gets a named refusal, not a skip.

## In scope

Slices 1–5, behind `view='fit'`, `out.check()`, `out.probe()` and
`fit.check(run_dir)`. Skills: update `precis-overview` and
`precis-toolpath-help`, and add `precis-se-fit-help` under
`src/precis/data/skills/`, each in the slice that ships the surface.

## Explicitly NOT in scope

- Binding and BOM (design-owned, gr514245 ruling).
- Assembly-sequence search; tilt-and-align insertion (`se-mechanical-drc.md`
  rulings stay).
- Tolerance stack-up and thermal tolerancing (fixed per-rule tolerance only).
- Hex-key / L-key sector access (`se-tool-sector-and-lkey-access.md`).
- `view='ops'` slicing (gr514235).
- Collision over a joint's range of motion (static pose only).
- Atomic-scale blocks (Å): fit is macro only; atomic blocks are listed as
  "not fit-checked".
- `rd`, blend and `field:` leaves (`export.needs_field_backend`): listed as
  "not fit-checked" until the field mesher is fast enough (Decision 12).

## Acceptance criteria

1. `fit.check(run_dir)` on the unicycle fixture reports, each with both
   parts' script lines: the wheel spokes in the wrong plane (overlap with
   axle, fork_left and fork_right, both cranks); bolts without holes (bore
   check, after adding `annotations.thread`); bolt heads on the wrong end.
   The run takes under 300 ms cold.
2. Every finding in criterion 1 also appears in `view='fit'` after import,
   and the view text equals `fit.check` apart from the header.
3. A missing or unknown material refuses mass with one message naming every
   offending block; the other checks still run.
4. A rebuild with a fix shows the interference under "cleared" with a mass
   delta; a new overlap shows under "new". Offline and server agree.
5. Two runs on the same input produce byte-identical reports.
6. `tests/test_se_insertion_path.py` and `tests/test_se_toolaccess.py` pass
   unchanged after slice 5, and the tool-volumes JSON is unchanged.
7. `view='clearance'` and drc on a macro-block design report no "no
   effective envelope" skips; templates appear neither as parts nor as
   warnings.
8. Import-linter passes with `precis.fit_classes` added to the wheel
   allow-list and no other new edge.
9. `scripts/test --selected` passes on each slice; no hostnames or IPs in
   fixtures or docs.

Post-deploy (ruling F): prod `unicycle-c1` shows gr514251's items in
`view='fit'` (slice 2).

## Fastener and part-library access

Reto: "Do we provide api access for fastener db, using mcp? ... also maybe
part library access."

**What the MCP offers today:**
- **Fastener standards:** `get(kind='component', view='series')` is the
  registry index. `get(kind='component', view='series', q='M5x16 socket
  cap')` resolves a free-text name: ranked, never picked, and head form and
  drive are reached through the family words. `get(kind='component',
  id='iso-4762', view='series')` gives one family's size table. `put(kind=
  'component', series='iso-4762', size='M5x16')` mints a component with its
  dimensions (thread, pitch, head, drive, length).
  `get(kind='component', id=<slug>, view='stock')` gives a supplier
  in-stock number. Skill: `precis-se-fasten-help` (the screw-choice table) and
  `precis-component-help`.
- **Components and parts:** `search(kind='component', q=...)` and
  `get(kind='component', id=..., view='tree'|'bom')` cover the engineering
  store. `search(kind='part', q=...)` is the JLCPCB/LCSC electronics catalog
  only. `search(kind='se', args={'wants': {...}})`
  (`precis_se/library.py`) is ranked search over blocks in live se designs,
  i.e. the design-block library.
- **Gaps:**
  - No structured fastener query such as "thread M5, length >= 16, head
    socket, drive hex": `q=` is free text and the size table is per family,
    so an agent walks families by hand.
  - No mechanical part library beyond what agents have minted
    (`component`) or built (`se` blocks).
  - None of this is reachable from a sandboxed script.

**Offline scripts:**
- ISO tables already ship in the wheel: `precis.component_series` is on the
  allow-list, and `precis.fit_classes` joins it (ruling B). A script can do
  `component_series` lookups with no network.
- For a part library, the options are a read-only MCP client in the
  sandbox, or a pinned snapshot.

  | | read-only MCP client in the sandbox | pinned snapshot (wheel or run dir) |
  |---|---|---|
  | works with no network or DB | no | yes |
  | reproducible | only if every answer is recorded | yes: the snapshot's sha goes in the manifest |
  | trust | the sandbox gets a credential and an egress path | none: data in, no channel out |
  | freshness | live | as of the snapshot |

  **Recommendation: a pinned snapshot.** Before the run, the job
  materialises the parts the agent asked for into
  `in/library.json`: series rows plus minted component rows with their
  sourced values, resolved through the MCP outside the sandbox. A new
  `precis_geom.library` reads it. The manifest records `library_sha`, and
  the importer refuses a run whose `library_sha` doesn't match what the job
  wrote. Standards tables stay in the wheel; the run-dir snapshot carries
  only the design-specific parts, so the wheel doesn't grow with the
  catalog.

  Follow-ups (not this spec):
  - `get(kind='component', view='series', args={'thread': 'M5',
    'length_min': '16 mm', 'head': 'socket'})` as a structured filter.
  - The snapshot step in the workspace job.



---
status: draft
title: se: built-in fit / interference / mass check and a probe primitive for scripted designs
pillar: 3d-design
prio: high
---

# se: built-in fit / interference / mass check and a probe primitive for scripted designs

Source: gr514325 (Reto approved the plan and the probe, 2026-10-24: "good,
try that"). Context gripes: gr514242 (no envelope on macro blocks),
gr514245 (no material/measure keys), gr514241 (template blocks count as
assembly), gr514251 (unicycle-c1 design bugs only geometry shows).

## Motivation / why

The unicycle dogfood agent rated the se views 3/5 and trusted its result
only because it wrote its own checker (`check.py`, ~100 lines): compose
instance poses from `manifest.json`, flatten through
`precis.cad.scene.build_design`, `bulk.volume` x a hardcoded density table,
bbox through the private `relate._bounds`, `relate.clearance` for connected
and near pairs. It found spokes 3 mm into the fork, bolts without holes and
bolt heads on the wrong end. The se views could not: macro (cad DSL) blocks
get no envelope on import (`precis_se/workspace_import.py::_block_envelope`
sizes atomic scenes only and has no call site for macro blocks), so
clearance, the DOF probe, mass and size all skip them.

Two further gaps: nothing checks that a hole exists where a fastener goes
(the "bolts had no holes" bug), and `toolaccess` (tool sweep) and the
fastener insertion path each carry their own swept-cylinder code. One
swept-volume primitive covers both and the bore check.

## Rulings (Reto 2026-10-24, "defaults")

- **gr514235:** `view='ops'` takes slicing args; the fold rule stays.
  (Not built here; recorded because slice (b) output reuses the slicing.)
- **gr514241:** `out.block(..., template=True)`, stored on the manifest
  block and the node. `validate` skips envelope checks for templates.
  Slice (a) consumes it (templates get no envelope, no check).
- **gr514245:** `material` and `measure` keys come first and are
  script-owned (`out.block(..., material=...)`, plus measure keys).
  Binding and BOM are design-owned and specced later: out of scope.
- Build order a -> c -> b, with d and e riding on b (gr514325 comment 1).

## Slices

| slice | what | Devin alone? | Opus seam review? | migration |
|---|---|---|---|---|
| (a) | envelopes from real cad at import | yes | yes (envelope semantics vs DOF/clearance consumers) | none |
| (c) | density from the material key; named refusal | yes | no | none |
| (b) | `se view='fit'` and `out.check()` | no, Devin builds the matrix; Opus owns the joint rules | yes | none |
| (d) | `precis_geom` `fit.check(run_dir)` | yes (after b) | no | none |
| (e) | `probe` primitive, bore check, re-express toolaccess | no | yes (rewrites two shipped checks) | none |

### (a) Envelopes from real cad at import (gr514242)

Files: `src/precis_se/workspace_import.py` (new `_macro_envelope(design,
block)` next to `_block_envelope`; call site in the macro-block branch of
the import loop), `src/precis/cad/relate.py` (public bounds, see b),
`src/precis_se/drc.py` / `compose.py` only if a consumer assumes
atomic-only envelopes.

API: no new verb. At import, a non-template macro block is built through
`build_design`, its AABB (public `relate.bounds`) becomes the block
envelope. Shape: oriented-box or sphere via the same canonical-metres
`ingest_envelope` seam as atomic blocks, whichever the existing envelope
grammar already has (AABB box if it exists; do not add a grammar). Template
blocks (`template=True`) get none and are exempt from
`block_without_envelope`.

Also in this slice: the manifest `template` key (add to `_BLOCK_KEYS` in
`precis_geom/out.py`, the importer, and `get(kind='se')` tree: templates
render under a "templates" group, not as pose-zero blocks).

Tests: `tests/test_se_workspace_import.py` (extend): macro block gets an
envelope that contains its cad AABB; template block gets none and raises no
`block_without_envelope`; a macro whose cad fails to build gets a named
refusal, not a silent skip; rerun on the unicycle fixture shows 0 skipped
blocks in `view='clearance'` and `drc`.

Migration: none if the envelope column/grammar already holds a box.
If the grammar lacks one, stop and ask (Questions 1).

### (c) Density from the material key (gr514245, scoped)

Files: `src/precis_geom/out.py` (`_BLOCK_KEYS` += `material`, `measure`;
vet `material` against the catalog on `out.block`/`finish`),
`src/precis_se/workspace_import.py` (store on the node meta),
`src/precis_se/properties/` (density lookup; reuse the `material` kind's
density, do not add a table), new helper `mass_properties(design, blocks)`
in the module slice (b) uses (suggest `src/precis_se/fit.py`).

API: `out.block(name, geom, material='steel-1045', ...)`. Mass =
`bulk.volume` x catalog density. Unknown or missing material: refuse the
mass computation with one message naming every offending block and the
nearest catalog matches; never a default density. The fit view still
reports interference and bbox when mass is refused (mass line carries the
refusal), so a missing material blocks only mass, not the other checks.

`measure` keys (e.g. bearing bore = 20 mm hard) are stored and echoed but
not consumed by fit in v1; they are the hook for the bore probe in (e)
(Questions 4).

Tests: `tests/test_se_fit_mass.py`: known box x known material gives the
expected grams; two blocks with missing/unknown material give one refusal
listing both; nothing falls back to a default.

Migration: none (node meta is JSON). Confirm.

### (b) `se view='fit'` (check.py productised)

Files: new `src/precis_se/fit.py` (engine, pure over a `Design` + manifest
data, no DB so (d) can reuse it); `src/precis_se/handler.py` (register
`view='fit'`, next to the existing views); `src/precis_geom/out.py`
(`Run.check()`); `src/precis/cad/relate.py` (rename `_bounds` ->
`bounds`, keep `_bounds = bounds` alias for one release; export in
`__all__`); `src/precis/cad/scene.py` only if flattening needs a helper.

Engine, in order:

1. Flatten: compose world poses (parents included), `build_design` through
   `use`, components grouped per instance (check.py lines 41-68). Template
   blocks are excluded.
2. Interference matrix: every instance pair; AABB broad phase on
   `relate.bounds` inflated by the pair's `min_clearance`/2 mm margin, then
   `relate.clearance` narrow phase min over component pairs. Parent/child
   pairs are skipped as in check.py. Each finding carries the script line
   (`made-by`) of both parts, resolved the way `eyeview._made_by` does.
3. Intended contact: `out.connect` pairs. Expected gap by joint class:
   press-fit <= 0 within tolerance; bearing seat exactly 0 within
   tolerance; prismatic/sliding > 0; fixed/bolted 0 within tolerance;
   revolute > 0 or 0 by declared class. A connect whose gap violates its
   rule is a finding ("axle/crank gap +5.0 mm, expected 0"). Any
   non-connected pair with gap < tolerance (touch or overlap) is a finding.
   Joint class comes from the connect's existing joint type
   (`precis_se/joints.py`); no new connect keys in v1 beyond optional
   `fit=` override (Questions 2).
4. Optional per-pair rule: `out.check(min_clearance={("a","b"): 0.002})`
   (metres, canonical bare) or the equivalent manifest key. Default none.
5. Mass properties from (c): per-block mass, total, CoG (volume-weighted
   with density), world bbox from `relate.bounds`.
6. Diff against the previous build: the importer already keeps the last
   run's `trace.json`; store the previous fit summary (finding keys +
   total mass) beside it and report new interferences, cleared
   interferences and mass delta. Ties to the change-summary-on-re-import
   ask (gr514325 item 5; `out.py::_trace_diff` is the existing pattern).

API: `get(kind='se', id='<design>', view='fit')`, one section per
step, findings as `severity | pair | gap | expected | lines`. In script:
`out.check()` returns the same report object and, at `finish()`, writes it
to `manifest.json` under `fit` (does not raise; `out.check(strict=True)`
raises on any error finding so `scripts/test`-style runs fail loudly).

Performance: pairs prune on AABB first; the narrow phase is capped (pair
budget, like `toolaccess._BudgetExhausted`) and reports "N pairs not
narrowed" rather than hanging.

Tests: `tests/test_se_fit_view.py` with a small synthetic fixture
(three blocks: seated pair, 3 mm overlap, 5 mm gap on a connect):
overlap and gap are found with line numbers of both parts, seated pair is
clean, press-fit with a -0.05 mm gap passes. `tests/test_cad_relate_bounds.py`:
`bounds` public, alias still works. Diff test: second build with the
overlap removed reports it cleared and a mass delta.

Migration: none.

### (d) `precis_geom` `fit.check(run_dir)`

Files: new `src/precis_geom/fit.py` (thin: read `manifest.json` +
`blocks/*.cad`, call `precis_se.fit` engine). Check the import direction:
`precis_geom` must not import `precis_se` if the package layering forbids
it; if so, put the engine in `precis/cad/` (e.g. `precis/cad/fit.py`) and
have `precis_se` call it (Questions 3).

API: `fit.check(run_dir, *, material_table=None) -> Report`; also
`python -m precis_geom.fit <run_dir>` printing the same text as the view.
No DB, no network. Runs before import so an agent iterates offline.

Tests: `tests/test_geom_fit_check.py`: the synthetic fixture from (b),
run offline, output equals the view's text.

Migration: none.

### (e) Probe primitive

Files: new `src/precis/cad/probe.py` (the primitive; `relate`-level, no
se imports), `src/precis_se/toolaccess.py` (re-express on it),
`src/precis_se/fasten.py` / insertion path (bore check; re-express),
`src/precis_geom/out.py` (`Run.probe`), `src/precis_se/fit.py` (include
probes in the report).

API:

```
probe(design, origin, direction, length, diameter,
      cone_half_angle=None, *, ignore=(), cull=None)
  -> list[Hit(block, component, depth, entry, exit, line)]
```

A straight cylinder (diameter) swept along `direction` for `length`,
or a cone when `cone_half_angle` is given (diameter is then the start
diameter). Hit `depth` is the swept length inside material per block;
`line` is the block's made-by script line. Implementation: swept solid
as a cad expression (as `toolaccess._swept_cyl` already builds it), AABB
broad phase over component bounds, `toolaccess`'s drive-face cull
(`_behind_the_drive_face`) generalised to an optional `cull` predicate,
then `relate.clearance(swept, component) < 0` narrow phase; depth from
a bisection or a few axial slices (decide in code, tolerance test below).
Answer to Reto's question: yes, rays need a diameter; a 0 diameter is the
thin-ray case and reuses `out.ray`'s semantics, so `diameter` is
required but may be 0.

First use, bore check per fastener: from the seat/axis of each fastener,
sweep (1) the clearance diameter (ISO 273 medium series for the size) through
every clamped member and expect zero hits there; (2) the thread major
diameter into the tapped member, expecting hits only inside the
declared thread depth. Any material hit in (1) = missing or misaligned
hole, reported with the fastener and member lines. Unicycle "bolts
without holes" must trip this.

Re-expression: `toolaccess.access` (tool shaft cylinder + swing disc)
and `insertion_path` (screw body sweep) call `probe`; their findings
and tests (`tests/test_se_insertion_path.py`, tool-access tests) must not
change. The refactor lands as its own commit after the probe has its own
tests, so a regression bisects to it.

Script-declared probes: `out.probe(name, origin, direction, length,
diameter, cone_half_angle=None, block=..., expect='clear'|'hit'|'n')`
for line of sight, sensor FOV (cone) and cable/tool paths. Reported in
`view='fit'` as pass/fail by `expect` (default `clear`). Relation to the
shipped `out.ray` / `pga.Ray` (observation rays with `bundle`, `kind`)
is Questions 5.

Tests: `tests/test_cad_probe.py`: cylinder through a plate with a hole
(no hit) vs without (hit, depth = plate thickness); cone widening hits
a block a cylinder misses; ignore/cull; tolerance of depth. Bore check
test in `tests/test_se_fit_bore.py`. Regression: existing insertion-path and
tool-access tests unchanged and green.

Migration: none.

## In scope

Slices (a)-(e) above, behind `view='fit'`, `out.check()`, `out.probe()`,
and `fit.check(run_dir)`. Skill docs: update `precis-overview` /
`precis-toolpath-help` and add a short `precis-se-fit-help` skill under
`src/precis/data/skills/` in the slice that ships each surface.

## Explicitly NOT in scope

- Binding and BOM (design-owned, specced later, gr514245 ruling).
- Automatic assembly-sequence search, tilt-and-align insertion
  (`se-mechanical-drc.md` rulings stay).
- Tolerance stack-up and thermal/clearance-class tolerancing: fixed
  tolerance parameter only.
- Hex-key / L-key access (`se-tool-sector-and-lkey-access.md`).
- `view='ops'` slicing (gr514235): ruled, built separately.
- Kinematic/dynamic collision over a joint range: static pose only.

## Acceptance criteria

1. Rebuilding the unicycle fixture and running `view='fit'` reports: wheel
   spokes ~3 mm inside the fork, bolts without holes (via the bore
   probe), bolt heads on the wrong end (head seated away from the
   clamped member, or the head-side probe blocked), each with both parts'
   script lines.
2. For prod `unicycle-c1`, `view='fit'` reports gr514251's items: axle
   5 mm short of both cranks (connect gap +5 mm, expected 0), asymmetric
   bearing placement, both pedals at x=+170.
3. A fixture with a missing/unknown material refuses mass with one
   message naming every offending block; the other checks still run.
4. `view='clearance'` and `drc` on a macro-block design report 0
   "no effective envelope" skips; template blocks do not appear as
   blocks or warnings.
5. A second build with a fix shows the interference in "cleared" and a
   mass delta; with a new overlap shows it in "new".
6. `fit.check(run_dir)` output equals the view's text on the same run.
7. Existing insertion-path and tool-access tests pass unchanged after
   the re-expression.
8. `scripts/test` selected pass on each slice; no hostnames or IPs in
   fixtures or docs.

## Target + blast radius

`precis_se` (workspace_import, handler views, drc, toolaccess, fasten,
properties), `precis_geom.out` (manifest keys, `check`, `probe`),
`precis.cad.relate` (public `bounds`), new `precis/cad/probe.py`, skills.
Post-deploy look: rerun the unicycle workspace import on the dev DB, then
`view='validate'|'clearance'|'drc'|'fit'`; check import time on a large
design (narrow-phase budget).

## Open questions / decisions log

(Not decided here; none blocks slice (a) or (c).)

1. Envelope grammar: does it have an AABB box, or only sphere/cylinder?
   (a) needs the answer; a sphere envelope makes the DOF probe and
   clearance coarser than the cad it came from.
2. Per-joint expected gap: take it from the connect's joint type only, or
   add an explicit `fit=` key on `out.connect` (press / bearing / slide /
   fixed)? Default tolerance value (0.05 mm? per-class?) needs a number.
3. Package layering: may `precis_geom` import the `precis_se` engine, or
   does the engine live in `precis/cad/`? Decides where `fit.py` goes.
4. `measure` keys: stored only in v1, or does the bore check already read
   them (bearing bore = 20 mm hard)? Which measure vocabulary?
5. `out.probe` vs shipped `out.ray` (observation rays with `bundle`):
   one primitive with `out.ray` as the thin case, or two? Preference:
   probe subsumes ray for hit queries; `out.ray` keeps its field-sampling
   role.
6. Bore check inputs: fastener size to clearance/major diameters comes
   from `fasten.py`'s tables; confirm those are the single source and that
   the unicycle's fasteners are declared as fasteners, not plain blocks.
7. Stored fit summary for the diff (slice b step 6): in node meta or a
   file beside `trace.json`? Prefer the latter (no migration).
8. `depth` for a hit: bisection exactness vs speed; acceptable error
   (suggest 0.1 mm).

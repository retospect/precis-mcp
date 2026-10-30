---
id: precis-workspace-help
family: se
title: precis — build an se design with a workspace script (out.run → import)
summary: the script loop for building an se design — precis_geom (pg/pga) + out.run writes scratch/out/<run> (manifest.json + trace.json + blocks/), then the import_workspace edit op lands it in a design. The out.* API verbatim (block/instance/connect/ray), every manifest key, atomic vs macro geometry forms, the cad DSL frame conventions (cyl base z=0 axis +z, box centred in x/y, rot = Rz·Ry·Rx, polar about the node's z), the t_ template-name convention, the trace asks, and what the manifest cannot yet say (no binding/BOM keys — design-owned).
answers:
  - how do I build a macro mechanical design by script workspace?
  - what do out.run / o.block / o.instance / o.connect / o.ray write to the manifest?
  - which keys does a manifest entry take (blocks, instances, connects, rays)?
  - how do I import scratch/out/<run> into a dev design and get() views on it?
  - what frame do cyl / box / polar / rot land a cad shape in?
  - why does view='trace' show no frames for my cad blocks?
  - can a workspace script set material, binding or a BOM?
  - how do I build and decorate a molecule by workspace script (fragments, functional groups, collapse check)?
applies-to: workspace scripts that build se/cad geometry (precis_geom out.* → the import_workspace op → get(kind='se'))
status: active
tags: [design]
kinds: [se, cad]
---

# precis-workspace-help — build a design by script

Build a mechanical design by script workspace: one Python script
builds the geometry, `precis_geom.out` writes a run directory, and
one trusted import lands it in an `se` design. The script
never touches the DB: it writes `out/manifest.json` plus one geometry
file per block and `out/trace.json` (the construction trace — which
line made which block). The importer vets the whole tree before the
one transaction that applies it.

Alias convention — every example uses them: `import precis_geom as
pg`, `import precis_geom.assembly as pga`, `import precis_geom.out as
out`.

## The loop

1. `main.py` builds geometry: atomic leg — hexfold scenes, RDKit, ASE;
   macro leg — the `precis.cad` DSL (source text or a `SceneSpec`).
2. `with out.run("<run>") as o:` declares the design — `o.block`,
   `o.instance`, `o.connect`, `o.ray`. On exit the run lands in
   `scratch/out/<run>/` (`manifest.json` + `trace.json` + `blocks/…`);
   a raised exception still writes a `status="failed"` manifest and
   the error-marked trace before re-raising.
3. `uv run python scratch/<run>/main.py` — the out dir is the whole
   record.
4. Import it into the design and read the views (the `import_workspace`
   op below). The sandbox `run` lane (a workspace ref built by a
   `sandbox_run` job) is not built yet — this host loop is today's
   path.

## Build molecules and decorated nanostructures — `mol.*`

Atomic blocks made from fragments, functional groups and generators use
`precis_geom.mol`, `precis_geom.steps` and `precis_geom.inspect` inside the
same `out.run`. The molecular loop is `mol.prism` / `mol.generate` /
`mol.frag` + `mol.lib` → `mol.decorate` (site grammar `open & face:out &
every:3`) → `mol.cap` → `mol.relax` (cached collapse check) → `o.block` /
`o.instance`. Every costly `mol` call is cached in `scratch/cache/`, so a
re-run after a crash or a recipe edit resumes at the first changed step;
`out.run` exit runs the chemistry check (`check.json`). The loop, the site
grammar, the cache, both checks and a runnable decorated-prism example are in
[[precis-workspace-mol-help]].

## `out.*` — the manifest calls

```python
import precis_geom.out as out

with out.run("hcol") as o:  # scratch/out/hcol/
    o.block(
        "p",
        scene,
        desc="carbon strip",
        ports=[{"name": "tail", "atom": "aC4", "roles": ["join"]}],
    )
    o.block("plate", "plate add box:w40mmd20mmh10mm")  # macro DSL text
    o.instance("pin", template="plate", parent="p", pose="[0 0 10] mm")
    o.connect("p.tail", "pin.top")
    o.ray(
        "bolt-a1",
        block="plate",
        origin=[0, 0, -0.003],
        direction=[0, 0, 1],
        length=0.028,
        kind="bolt_axis",
    )
# exit: manifest.json + trace.json written
```

- `o.block(name, geom, *, scale=, ports=, relax=, desc=, mode=,
  material=, measures=, loads=, template=, frame=)` —
  `geom` decides `scale`: a structure `Scene` → `scale='atomic'` →
  `blocks/<name>.extxyz` (bonds ride `blocks/<name>.bonds.json`); a
  cad `SceneSpec` → `scale='macro'` → `blocks/<name>.cad.json`; DSL
  source text → `scale='macro'` → `blocks/<name>.cad`, parsed by the
  importer's same `parse_source`. `scale=` may be passed explicitly
  but must agree with what `geom` looks like.
- `o.instance(name, *, template=, parent=, pose=, rot=, pose_unit=,
  rot_unit=, linear=, polar=)` — a placement of a block *by name* (or
  a symbolic `pga` Geom — `pga.tile` / `pga.instance` on a template
  name — whose `linear:`/`polar:` array spec and `pose` land verbatim
  in the entry). `parent` nests the instance under another block *or
  another instance*.
- `o.connect(a, b, *, kind=, joint=, objectives=)` — an intent edge
  `blk.port` ↔ `blk.port`.
- `o.ray(name_or_ray, ..., block=, frame=)` — an observation ray on a
  block this run declares (a `pga.Ray`, or `pga.ray` arguments).
- `o.finish(notes=)` — the explicit form when the `with` doesn't fit.
  A re-run over the same out dir diffs `trace.json` against the
  previous one and echoes `vs previous: +A -R ~C`.

Every `frame=` defaults to that call's own trace-frame id — the
manifest↔trace join that makes `made-by:` and `view='trace'` work.

## Instances and names — the `t_` rule

Blocks and instances share one namespace: an instance may not reuse a
block's name. Nothing vets it today, so `o.instance("wheel",
template="wheel")` surfaces later, op-side, as the generic
"duplicate block name" (gr514240). The convention: name a block that
exists only to be instanced `t_<part>` (`t_spoke`), and let the
placements take the plain names (`spoke`, `spoke2`, …).

TODO gr514241 — `out.block(..., template=True)` will mark a
template-only block on the node itself so validate skips its envelope
checks and the tree can hide it; until that flag lands, the `t_`
prefix is the marker.

## The cad DSL frame conventions

Macro geometry is the line DSL — `<name> <op> <config> [@x,y,z]
[rot:…] [polar:…|linear:…]`, `op` ∈ `add`/`cut`/`intersect` — or the
same spec as `.cad.json`. The conventions that decide where a shape
lands, each fixed in one place (`precis.cad.vec` for the transform,
`precis.cad.scene`/`primitives` for the shapes):

- **`cyl:r<R>h<H>`** (and `cone:`/`tcone:`/`hex:`/`ngon:`/`frustum:`/
  `pyramid:`): base at `z=0`, axis **+z**, standing `h` up — the
  frustum family is one class spanning `z=0` to `z=h`. `rot:` tips the
  axis, the base goes with it.
- **`box:w<W>d<D>h<H>`**: centred in x/y, base at `z=0`. Centre in z
  too with `@x,y,-h/2`.
- **`sphere:r<R>`** is centred at the origin — the one exception to
  base-at-`z=0`.
- **`rot:rx,ry,rz`**: `R = Rz·Ry·Rx` — rotate about local x first,
  then y, then z (`precis.cad.vec.rotation`; radians inside, explicit
  `deg`/`rad` per component at the text boundary). Read the composite
  right to left: `rot:90deg,0deg,0deg` turns the +z cyl axis onto the
  y axis (`z → −y`, the right line for an axle), while
  `rot:0deg,90deg,0deg` lays it along x. The order bites once two
  angles are non-zero — it is not "pitch then roll", it is x applied
  first.
- **`polar:nNrR`**: `n` copies of the node spaced `360°/n` on a
  circle of radius `r` about the node frame's **z axis**, at the
  node's `@z` — copies land at `(r·cos θ, r·sin θ, z)`, each carrying
  the node's `rot`. The node's `@x,y` is ignored (write `@<r>,0,<z>`
  so the source reads like the first copy's own position). An
  unturned `polar:` ring always lies in the xy plane —
  `spokes add box:… polar:n12r0mm` under a wheel whose axis is y puts
  the spokes 90° wrong, and nothing complains until a clearance check
  does. Copies are named `<name>#1…`.
- **`linear:nNdx..dy..dz..`**: `n` copies stepped `(dx, dy, dz)` per
  copy from the node's `@x,y,z`.

Units: every dimensioned token needs one — `@0mm,0mm,-1mm`,
`polar:n6r18mm`, `rot:0deg,0deg,45deg`, `box:w40mmd20mmh10mm`
(`mm|cm|km|nm|Å|in|ft|m`; `deg|rad` for angles). A bare number raises
`UnitRequiredError` with a hint echoing the value under plausible
unit readings — the zero-counting guard doing its job. Stored
`config`/pose text is always canonical SI: bare metres, bare radians.

Instance `pose`/`rot` (manifest entries and ops alike — one grammar):
the canonical form is one trailing unit on a 3-vector (`"[0 0 10]
mm"`, `"[0 0 90] deg"`); per-component units also work
(`["3nm", "0nm", "2Å"]`); or pass bare numbers plus the sibling
`pose_unit=`/`rot_unit=` key (`"mm"`, `"rad"`). A bare nonzero vector
with no unit anywhere is refused.

## The manifest — what `out.*` writes

`manifest.json` is `{"schema": 1, "blocks": [...], "instances": [...],
"connects": [...], "rays": [...], "notes"}`. An unknown key anywhere
is refused by name — the vet is the contract, and the key set is:

| entry | keys |
|---|---|
| block | `name`, `file`, `scale` (`atomic`\|`macro`), `ports`, `relax`, `desc`, `bonds`, `bonds_sha`, `mode`, `material`, `measures`, `loads`, `notes`, `template`, `findings`, `census`, `relaxed_by`, `frame`, `sha` |
| port (inside `ports`) | `name`, `atom` (atomic only), `roles`, `direction`, `expected_element`, `expected_hybridization`, `pose`, `annotations`, `frame` |
| instance | `name`, `template`, `parent`, `pose`, `rot`, `pose_unit`, `rot_unit`, `linear`, `polar`, `frame` |
| connect | `a`, `b`, `kind`, `joint`, `objectives`, `frame` |
| ray | `name`, `block`, `origin`, `direction`, `length`, `kind`, `fields`, `step`, `reach`, `in`, `anchor`, `bundle`, `frame` |

Every `frame` is that entry's maker trace-frame id. A block's `sha`
is its written file's content hash; a re-import compares it with the
bound ref's last one to report the block unchanged or changed. Port
`annotations` carries `diameter`/`depth` (metres, positive) and
`thread` (text like `'M5x0.8'`). A `rays` entry replaces the
workspace's previous rays wholesale — one the run stops writing
retires. A macro port's `atom` is refused (atoms are atomic-only).
A block's `mode`/`measures`/`loads` are its manufacturing mode, named
measures and load declarations — replayed on import as the
`set_mode`/`add_measure`/`set_load` ops; `template` (`true`) marks a
library part that is never itself placed (gr514241). A block's
`material` is its explicit `kind='material'` slug — replayed as
`set_material`; it wins over `mode`'s `family/material` half, which
stays the fallback (gr514245). A block's
`findings` is a list of strings — the Geom findings `out.block` stamps
(`chirality.flipped` and the like); `census` is an object — a hexfold Net's junction/seam/valence summary `out.block` stamps (gr514059), run-record only, never imported; `relaxed_by` is `{rung, verdict, key?}` —
`out.block` stamps it on a Geom still holding `mol.relax`'s collapse-check
coordinates, and `view='validate'` shows `relaxed_by uff ok` so that check
is never read as the service relax; import echoes each finding as
`block '<name>': <finding>` so a mirrored hand shows in the run record
(gr514205). `sha` is the writer's sha256 of the geometry file and
`bonds_sha` the sidecar's — `out.block` stamps both automatically
(`bonds_sha` only when the writer serialized the sidecar itself;
`bonds`/`bonds_sha` are atomic-only). Declared means verified: the
imported bytes must hash to the declared digest or the whole import
refuses, naming the path and both digests — the inline `run` map
retypes every byte, and this is the check that catches a corrupted
upload (gr517643). `sha` also lands on the bound
ref's `meta.generated.sha`, so a re-import can answer
changed/unchanged without re-diffing geometry (gr514248).

## Ledger notes — `notes`

A block's `notes` is a list of `{"name", "kind":
"question"|"answer"|"decision", "text", "re"?, "about"?, "origin"?}`
— `add_note` ledger entries (`about` defaults to the declaring
block, `origin` to `generated`); the ledger is append-only, so a
re-imported identical note is skipped and a same-named different one
refuses (gr514245).

**Not yet supported.** The manifest has no `binding` or `bom` keys —
the vet refuses them as unknown. Ruling (Reto 2026-10-24): binding and
BOM are design-owned — write them on the design after import with the
ordinary ops (`set_binding`, BOM ops in [[precis-se-help]]). The
script-owned `material`/`measure` keys landed (gr514245).

## Two scales, one design — Å vs macro

| | atomic leg | macro leg |
|---|---|---|
| geometry | `Scene` (atoms/bonds) → `.extxyz` + `.bonds.json` | DSL text → `.cad` · `SceneSpec` → `.cad.json` |
| library | hexfold (`pg`/`pga` `Geom` in Å), RDKit, ASE | `precis.cad` primitives (`pga` `Geom` in mm) |
| bound ref kind | `structure` | `cad` |
| trace reads | per-atom `maker` → `view='trace'` | `view='trace'` summary/frame/`against` + `made-by:` (below) |

A block binds either leg; one design can mix both — instances and
connects don't care.

## The construction trace — what `view='trace'` reads

`out.run` traces every `out.*` call and the user functions around
them (`with trace("<name>"):` from `precis_geom.trace` names a user
frame without a function boundary — optional sugar); the importer
stores `trace.json` once as a `ws_trace` chunk on the run job. The
asks, all under `get(kind='se', id=…, view='trace', args=…)`:

```python
get(kind='se', id='uni', view='trace')                                # run summary
get(kind='se', id='uni', view='trace', args={'atom': 'p/w3/(2,1,A)'}) # who placed it
get(kind='se', id='uni', view='trace', args={'atom': 'p/s1/s1', 'touched': True})
get(kind='se', id='uni', view='trace', args={'atoms': ['p/a', 'p/b']})  # LCA frame
get(kind='se', id='uni', view='trace', args={'frame': '<key>'})         # frame card
get(kind='se', id='uni', view='trace', args={'against': <run_job>})     # run diff
```

`frame:<key>` then doubles as a region selector (datums, pockets, the
eye — [[precis-se-regions-help]]), `view='validate'` finding rows
carry the script line, and the block eye's `made-by:` names the user
frame (`ws-<run>::<fn> (main.py:N)`). A frame minted inside a library
keeps its internal `file`/`line` and also carries
`caller_file`/`caller_line` — the nearest call site outside that
package (the script line that drove it); the atomic leg's
`blocks/*.extxyz` joins back per atom through its `maker` column,
which is the frame id that minted the atom (a merged atom keeps the
earliest frame's stamp).

**Macro leg.** `view='trace'` answers on cad-bound macro blocks too —
the summary, `frame` cards, `against` run diffs, and a `made-by:`
listing (which script line made which block). Only the *atom* asks
(`atom`/`atoms`) stay structure-bound: a solid has no atoms to trace.

**build123d / CadQuery limit.** The importer accepts only the
`precis.cad` graph, so a build123d script converts at the end and gets
frames only from the conversion call — the OCP internals are
untraced.

## Cite the DB in the script — `# <handle> (<why>)`

A workspace script gets read later by someone with a different DB or
none, so every decision that came out of precis carries its source in a
comment — a compact handle plus a few words of why, on the line where
the decision lives:

```python
r_pu = b.part("R_PU_SDA", label="RES-0402-4k7")  # fi349615 (I2C pull-up sizing at 400 kHz)
vbus = b.net("VBUS", cls="power", current=5.0)   # gr513665 (PDO table: 20 V fixed)
u1 = b.part("U1", lcsc="C14877")                 # pa481516~chunk3 (sleep current vs rail budget)
```

Annotate a part choice, a value taken from a datasheet (decoupling,
dropout, thermal), a constraint number (clearance, creepage, trace
width), and any weight or param a paper/finding set. One handle plus the
why — short enough that the code still reads as code. A citation into a
chunk of a document uses the `~` selector form (`pa481516~chunk3`,
`ds:<slug>~p<n>`).

A `# <handle>` comment is an annotation only when the handle is the
comment's **first** token (`# see gr513665 for history` is prose, and
external forms like `# lcsc:C6186`/`# ds:<slug>` are never handles —
they stay prose). The import op lints every `.py` file it syncs:
`import_workspace` echoes `ref-lint:` with `file:line` for every
handle that does not resolve — a warning, never a refusal. On export,
`expand_references` (`precis.provenance.refs`) rewrites each resolved
handle to a self-contained citation (title / part number / DOI / page
+ a capped verbatim excerpt) as `#|` inline details, a `REFERENCES`
comment block, or a `REFERENCES.md` sidecar; internal handles (gr, td,
me, …) become `(internal ref, not exported)` so no internal id leaks,
and `strip_references` reverses the pass on import (gr514049).
Generators that emit workspace code (an EasyEDA → script rewrite)
write the same convention.

## Import a run — the `import_workspace` op

One `edit(kind='se')` carries the run's `out/` tree inline as
`{relpath: text}` — nothing on prod needs a host path:

```python
get(kind='se', id='<design>')               # precheck: expect not-found
put(kind='se', id='<design>', title='…', text='{}')
edit(kind='se', id='<design>', ops=[{
    'op': 'import_workspace',
    'workspace': '<workspace-slug>',
    'run': {'manifest.json': '…', 'trace.json': '…',
            'blocks/p.extxyz': '…', 'blocks/p.bonds.json': '…'},
    'files': {'main.py': '…'},   # optional — syncs the code folder
    # 'dry_run': True           — the plan, nothing written
}])
```

`run` is the whole `out/` verbatim (`manifest.json` + `trace.json` +
`blocks/*`; the `.fitcache/` cache dir may be dropped) — and the
files' bytes are verified: a declared `sha`/`bonds_sha` that does not
match the uploaded file refuses the import, an empty bonds list or a
`files` `.py` that does not compile refuses naming file:line
(gr517643). `edit(ops=[...])` is the canonical call form — `ops=` is a
declared kwarg of `edit`, not a deprecated spelling.

**Staged door.** Multi-KB `out/` trees should never ride the inline
map: `ops=[{'op': 'workspace_upload_link'}]` (sole op, same
`edit(kind='se')` door) mints a single-use signed URL; `tar cf
run.tar -C out .` then `curl -f --data-binary @run.tar` POSTs the
bytes straight to precis_web — they never pass through model tokens.
The answer's sha256 then goes in as `'staged': '<sha256>'` instead of
`'run'` (same caps, same manifest-sha verification — the staged tar
is what `sha`/`bonds_sha` were made to vouch for). `files` syncs
the workspace's code: a `folder` ref `part-of` the design plus
`plaintext` children slugged `ws-<workspace>--<path>` — the
`ws-<workspace>` python root then serves them (`get(kind='python',
id='ws-<workspace>::<qualname>')`). Absent `files` leaves the folder
untouched; `files={}` retires every file.

**Main/prod drift.** The manifest surface on main can be ahead of the
deployed release — `census` (gr514059) landed after r106, so prod
r106's vet refuses a manifest carrying it as an unknown key. When a
fresh `out/` refuses on prod naming a key this table lists, that key
is post-release: strip it for the prod import (it is run-record only)
or wait for the next release.

The op rides alone — the sole op of its `edit`, refused in `put` (put
builds a fresh tree). An unknown design's error `next=` points at
`put(kind='se')`. On `target='design'` (the default) a design holding
blocks this workspace did not make is refused outright;
`target='fork'` imports into a copy `<design>--<workspace>` linked
`derived-from` instead, when the existing design must stay untouched.
Re-importing the same workspace replaces what the last run made and
retires what it dropped — a same-named block is rewritten in place
(ports/connects re-resolve; a vanished port is a `port.missing`
finding), a dropped one's bound ref retires with it and `restore_ref`
brings it back. A failed run's `status="failed"` manifest is a
record, not an import — the vet refuses its `status`/`error` keys;
read `manifest.error.at` and the error-marked frames in `trace.json`
directly.

## Refusals worth knowing

- An unknown manifest key → `BadInput` naming the key and the
  accepted set; a bad `scale` lists `atomic`/`macro` as options.
- `trace.json` over 8 MB → refused; over 20k frames the importer keeps
  the head and reports `trace.truncated`.
- A block `file` path escaping `out/` → refused by name.
- `UnitRequiredError` from a macro block's DSL escapes as-is: it names
  the value and the unit hint but not (yet) the block or DSL line.
- An instance whose `template` resolves to nothing → op-side refusal
  naming it; a connect endpoint that is not `blk.port`, same.

## See also

- [[precis-diag-help]] — a failed run's structured record: the Diag anchored on the error-marked trace frame
- [[precis-workspace-optimize-help]] — `precis_geom.physics.evaluate`: score a script-built shape
- [[precis-workspace-mol-help]] — molecular builds: fragments, decorators, step cache, collapse check, `pg.inspect`
- [[precis-se-help]] — the se call surface: ops, views, units, BOM
- [[precis-se-atomic-help]] — the atomic leg: chemistry over the block tree
- [[precis-cad-help]] — the cad design kind: DSL grammar, parts catalog, exports
- [[precis-cad-assembly-help]] — connect cad parts by interface (`port`/`mate`/`joint`)
- [[precis-job-observe-help]] — `sandbox_run` and the compute-lane job types
- [[precis-se-regions-help]] — pockets, measures, `frame:` selectors, observation rays

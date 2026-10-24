---
status: draft
title: Per-atom maker map, seam/junction atom sets and a valence report in a workspace run's out/
pillar: 3d-design
prio: normal
blocked-by: trace-frame-structural-keys
---

# Atom maker map, seam/junction sets, valence report

Sibling of [trace-frame-structural-keys](trace-frame-structural-keys.md):
that item makes a frame's key survive edits; this one makes the key
reachable from `out/` per atom, and adds the two structural summaries a
script author otherwise recomputes by hand. Sources: gr514054 (no
atom -> frame join from `out/`) and gr514059 (no seam/junction sets, no
valence summary). Context: [structure-workspaces](structure-workspaces.md)
§Construction traces (the per-atom `maker` column is specced there; this
item is what the core wheel must write for it).

## Motivation / why

From the dogfood run `hexprism p6` (4 periods, r4, s2):

- `trace.json` has 40 frames with `made` counts only. The extxyz carries
  species, position and `label`, nothing per atom saying which frame
  placed it, so "which builder call made atom 217" cannot be answered
  from `out/` (gr514054 c).
- Core frames' `file/line` point into `hexfold/prism.py`, not the script
  (a); the `.hx` path has no frames (b). Both are capture questions, see
  Out of scope.
- `net.report.ok=True` while the degree histogram is `{1:24, 2:96, 3:336}`:
  24 degree-1 stubs and 96 open-valence edge atoms. The author recomputed
  degrees by joining `bonds.json` label strings (gr514059). `net.junctions`
  records exist (`hexfold.build.JunctionRecord`: `name`, `fused`, `faces`,
  `atoms`) but `out/` does not export them; `bonds.json` has no order or
  kind and no seam marking.

## Design

All three artifacts are written by `precis_ws.out.block()` beside
`blocks/<name>.extxyz`, per block, from data the build already holds. No
new compute, no DB, wheel-only (the `precis-geom` import allow-list in
`tests/workspaces/test_geom_wheel_imports.py` stays satisfied).

### 1. Maker map

`blocks/<name>.makers.json`:

```json
{"schema": 1, "frames": ["a1b2c3d4", "9f8e7d6c"], "atoms": [0, 0, 1, 1, 1, 0]}
```

`frames` is the table of **structural frame keys** (the sha8 of
`(parent_key, qual, occ)` from the sibling spec), `atoms[i]` an index into
it for atom ordinal `i`, so the file is one small int per atom and a few
distinct keys (run-length encoded when a block exceeds 10k atoms). The
key is the maker id; it is the same string `trace.json` frames carry in
`key`, so the join is `trace.frames[key]` for qual, `line_in_def`, `src`,
`made`. A second column, `touched`, is not written: "which frames changed
this atom" stays a scan of the trace's `touched` ranges, as specced.

The same per-atom id also rides the extxyz as the `maker` column the
structure-workspaces spec names (`precis_geom.extxyz::to_extxyz`), written
as the sha8 string so a single extxyz is self-describing. The sidecar is
the compact form for tools that do not parse extxyz; if both are kept in
sync by one writer there is no drift. (Open question 1.)

How atoms get a maker: the traced primitive that returns the entity stamps
the leaf frame's key on it (structure-workspaces §Construction traces
"Capture"); `_block_from_net` carries the stamp from `Net.atoms` into the
block. Atoms with no stamped frame (the `.hx` path, untraced builders) get
`null`, written as index `-1`, and the report counts them: `unmade: 24`.

### 2. Seam / junction sets

`blocks/<name>.sets.json`:

```json
{"schema": 1,
 "junctions": [{"name": "reducer:6->4:outer", "atoms": [..], "fused": [[a, b], ..]}],
 "seams":     [{"name": "seam0", "atoms": [..]}],
 "sheets":    [{"name": "sheet0", "atoms": [..]}]}
```

Straight from `Net.junctions` (`JunctionRecord`: `name`, `fused`, `atoms`;
`faces` omitted, derivable), `Net.seam_rims` (seam atoms) and
`Net.sheet_atoms`, which the build already computes. Ordinals index the
block's extxyz rows. Each set also carries the maker keys of its atoms as
the set of distinct `frames` indices (`"made_by": [3, 7]`), so "which call
made this seam" is one lookup.

`bonds.json` gains, per bond, `order` (1.0 / 1.5 / 2.0 when the net knows
it, else omitted) and `seam: true` for a bond inside a seam or junction
set. Existing readers ignore unknown keys; `schema` bumps to 2 on the
bonds sidecar.

### 3. Valence report

`blocks/<name>.valence.json` (and a 6-line echo in the run summary):

```json
{"schema": 1, "element": {"C": {"degree": {"1": 24, "2": 96, "3": 336}}},
 "open": {"count": 120, "atoms": [..], "by_maker": {"a1b2c3d4": 96, "9f8e7d6c": 24}},
 "stubs": [..], "expected": "sp2-carbon: 3", "unmade": 0}
```

- Degree histogram per element from the bond graph.
- **open**: atoms below the expected coordination for their element and
  hybridisation (carbon sp2 net: 3). The expected value is a table in the
  writer (C 3 for sp2 nets, 4 where the net records sp3 hosts), not a
  chemistry engine.
- **stubs**: degree-1 atoms listed separately (usually a bug, not an edge).
- `by_maker`: open atoms grouped by maker key, so the report says which
  builder call left edges open. This is what the maker map buys.
- It reports; it does not fail the run. The net's own `report.ok` stays as
  is, and the valence line says plainly "unterminated: H capping is a
  `precis_se` step (`hexfold_spec._terminate_open_edges`), not in this
  wheel", so `ok=True` with 96 open atoms is explained, not alarming.

Whether `_terminate_open_edges` should move to the wheel is a separate
question (Open question 3); the report works either way.

### Run-to-run diff

`out.run`'s `vs previous` note (gr514055, separate item) can quote the
maker table: "atoms 456 -> 612 made by `prism>strip` (key a1b2c3d4)". Not
part of this item; the maker file just makes the data available.

## Slices (smallest first)

| # | slice | test |
|---|---|---|
| 0 | Prerequisite: structural frame keys (`trace-frame-structural-keys` slice 1) | its own tests |
| 1 | Stamp leaf-frame key on atoms in traced primitives; carry through `_block_from_net`; `makers.json` writer in `precis_ws.out.block` | tmp script: every atom of a strip maps to the `strip` frame key; untraced path gives `-1` |
| 2 | `maker` column in `precis_geom.extxyz` (writer + reader round trip) | to_extxyz -> from_extxyz keeps makers; old files without the column still load |
| 3 | `sets.json` from `Net.junctions`/`seam_rims`/`sheet_atoms`; `bonds.json` order and `seam` flags, schema 2 | `hexprism`-style fixture: junction count and atom sets equal `Net.junctions` |
| 4 | `valence.json` + run-summary echo, `by_maker` grouping | fixture with known open edge: degree histogram, open count and grouping exact |
| 5 | Goldens: add the three sidecars to `tests/workspaces/data/*/out` and the import test; importer reads `makers.json` into the `maker` column (replaces the extxyz-only path) | goldens pass; second run with an inserted comment line keeps all maker keys |
| 6 | Skill text: `precis-se-help` / `precis-hexfold-help` "reading out/": what each file answers | skill lint |

Slices 1 and 3-4 are independent of each other once slice 0 exists;
slice 4 works without slice 1 (everything lands in `unmade`/no grouping).

## Explicitly NOT in scope

Tracing core internals so frames point at the script line instead of
`hexfold/prism.py`, and frames for the `.hx` build path (gr514054 a, b):
a capture-granularity decision on `@traced`, filed against the trace
item. H termination in the wheel. Changing `report.ok` semantics.

## Open questions

- Keep both the extxyz `maker` column and `makers.json`, or only the
  sidecar (extxyz stays plain species/pos/label)? Proposed: both, one
  writer.
- Maker id is the sha8 key; should the file also store the readable
  `qual` + `line_in_def` per key so `makers.json` stands alone? Cost: a
  few hundred bytes. Proposed: yes, as a `frames` table of objects instead
  of strings.
- Move `_terminate_open_edges` into `hexfold` so a script can terminate
  and re-check, or keep termination server-side?
- Expected-valence table: carbon only for now, or element-generic from
  `precis.structure` covalent data?
- Block size cap for the `atoms` arrays (456 atoms here, 1.7k in the
  scene case, 100k+ imaginable): run-length encode above what threshold?

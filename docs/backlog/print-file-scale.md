---
status: ready
title: Scale factor on the se print 3MF writer and route
thread: se-machine-design
model: sonnet
pillar: 3d-design
prio: normal
---

# Scale factor on the se print 3MF writer and route

## Motivation / why

Every 3MF writer converts metres to millimetres 1:1, so a nanometre or micrometre se block exports as sub-micron geometry. Reto wants scaled print files: atomistic blocks scaled (e.g. 1e7), 1 µm–1 mm blocks fit to ~100 mm, above 1 mm 1:1. se-3d-viewer owns the dialog that suggests the factor; this item owns the writer, file name, metadata, route parameters and the print check at printed size.

## In scope

1. `scale: float = 1.0` on `write_mesh` and `_write_3mf`. Geometry is multiplied by `scale` after the m→mm conversion. 3MF metadata gains `precis:scale_factor` (the factor as written, e.g. `2e7`).
2. The print check (floating islands, slicer cantilever, mesh cleanup, package check) runs on the mesh at its printed (scaled) size, so it judges what the printer makes.
3. File name: `<design>-<block>-x<factor>.3mf` when scale ≠ 1 (factor formatted like `2e7`, `100`); unchanged at 1.
4. Route: `GET /se/{slug}/print/{block}.3mf?scale=<f>&model=<vdw|ballstick>`. `model=` is accepted and passed through only for atom blocks; until printable-atomic-models lands, an atom block answers 409 "not printable yet". A scale below the printable floor answers 422 with JSON `{"min_scale": <f>}` (Reto C1: refuse and name the smallest factor that prints).
   - The floor is the atom models' strut floor, which printable-atomic-models defines. A solid block has no floor: `min_scale` is `null`, and a too-small solid is reported by the print check at printed size, not refused.
5. `GET /se/{slug}/print/{block}.json` returns `{real_size_mm: [x,y,z], suggested_scale, min_scale, models: [...]}` for the dialog. Suggested factor: fit the longest side to 100 mm, rounded DOWN to a 1-2-5 step (…1e7, 2e7, 5e7…), never below `min_scale`; solid blocks above 1 mm suggest 1, or a reduction only when larger than the print bed. The 1 µm / 1 mm thresholds only pick the suggestion, never the formats offered.
6. Handler: `view='print'` accepts `args={'scale': f}` with the same semantics.

## Explicitly NOT in scope

- Atom geometry (consumed by `docs/backlog/printable-atomic-models.md`, not written here).
- Per-element colour (later, Reto D1).
- Dialog UI (se-3d-viewer).

## Acceptance criteria

- A test writes a 1 mm cube at scale 100 and reads back a 100 mm cube with `precis:scale_factor` = 100 in the package.
- The route returns the `-x100` filename.
- `?scale=` below `min_scale` returns 422 with `min_scale`. Until atom models exist, test it with a stubbed floor.
- The `.json` endpoint returns a 1-2-5 suggested factor for a 5 nm block that fits ≤100 mm (expect 2e7 for 5 nm).
- The print check reports at printed size (a feature below the slicer threshold at 1:1 passes at a scale that makes it printable).

## Target + blast radius

- Existing writer: `precis/cad/export.py::_write_3mf`.
- Route module: `src/precis_web/routes/se_print.py` (lands with the print-check branch).
- Print check integration: `precis_se/printing.py::export_block_mesh`, `write_mesh(built=, source_pitch=, title=)` (lands with the print-check branch).
- Depends on: the print-check + download-button branch, which provides the above interfaces.

## Open questions / decisions log

None.

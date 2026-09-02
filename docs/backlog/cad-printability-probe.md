# CAD: printability probe (FDM orientation search + process DRC)

Scoped 2026-09-02 with Reto ("for the 3d print implementer, can we build
it?"). A cad-level probe — `view='printability'` — that the future se
kind's slice-5 implementer rents (se-kind.md L5), buildable now because
it needs only a tessellated mesh, nothing from se's schema:

- **Orientation search**: score candidate build orientations
  (axis-aligned ± a coarse rotation sweep) by overhang-violation area,
  bed-contact area, part height, and bridge count; report the best
  build frame (its own "down", distinct from the part's working frame).
- **Process DRC per orientation**: faces steeper than the overhang
  limit (45° default), unsupported bridge spans over the limit,
  bed-contact area below the minimum. Findings in the
  structure-validate shape: rule / where / measured / expected /
  `suggested_fix`.
- **Rule values from data, not constants**: the FDM numbers validated
  2026-09-02 (`perplexity-reasoning:292746`) — 45° overhang at
  0.15–0.25 mm layers, bridge ~10 mm (ASA-class) / ~5 mm (TPU), line
  width 0.42–0.48 mm for a 0.4 mm nozzle — as a small capability dict
  with `source`/`retrieved` per field (seed of the se
  `se_capabilities.json` shape; move it there when se slice 5 lands).

Owner `src/precis/cad/` (probe over `tessellate.py` meshes) +
`src/precis/handlers/cad.py` (view wiring). Sequence AFTER the chamfer
+ connectivity-lint slice lands.

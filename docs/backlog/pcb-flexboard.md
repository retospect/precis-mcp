# Flexboard support in the pcb kind (idea)

Reto, 2026-09-14 (glowing-zooming-glade design session, alongside
`pcb-se-binding.md`): pcb should eventually model flex and rigid-flex
boards. Today the kind is rigid-only (v1 stackup docstring: "4-layer
rigid"; zero flex concepts in `src/precis/pcb/`). Owner anchor:
`precis.pcb` package docstring when this ships.

**The split that keeps everything else true.** Fabrication stays flat:
flex gerbers are flat mm 2.5D, so the mm-enclave ruling and
`mechanical_profile` as the single ×1e-3 crossing (map §Units policy)
are untouched. What flex adds is a *fold model* on top of the flat
design — and the installed (folded) configuration is se-side state,
not pcb data: pcb owns the flat outline, fold-line locations, and
material limits; se owns fold angles, exactly as it owns world pose
(`pcb-se-binding.md` §one-way truth). Fold lines project into se as
joints (hinge × bend radius) between board-segment sub-blocks;
multiple install configurations ride design-state-core's shared
states machinery.

**pcb-side needs (flat domain):**

- `board_type` ∈ rigid | flex | rigid-flex on `pcb_boards`; flex
  stackup entries (polyimide, coverlay) in the existing `stackup[]`.
- **Slice 0 — honor `keepout` features** (found 2026-09-14, Reto's
  "fold zone layer" question): migration `0047_pcb_kind.sql` already
  reserves `ftype='keepout'` but ZERO code consumes it — placer,
  router, and DRC know only NO_NET via keepouts and courtyard
  polygons. Implement region-keepout consumption first; it pays off
  on rigid boards too (antenna zones, connector clearance), and a
  fold zone is then a keepout subtype rather than new machinery.
- Fold-line feature (`ftype='fold'`, a keepout subtype after slice 0):
  line segment on the outline domain + bend radius + direction
  (+ allowed angle range), zone widened by the bend allowance;
  carries the height *threshold* (tiny components allowed, tall ones
  not), via ban, and copper-direction preference. Segments the
  outline into the rigid/stiffened regions `pcb-se-binding.md`'s
  derivation returns.
- Stiffener regions (a feature): where components are allowed on an
  otherwise bendable area.
- **Fold-zone DRC — the rule that makes folds real** (Reto
  2026-09-14): a fold only works where there are *no or tiny*
  components. The bend zone (fold line widened by the bend allowance,
  ≈ radius × angle arc plus margin) must contain no instances above a
  small height/size threshold, no vias, and ideally copper routed
  perpendicular to the fold axis. Violations are DRC errors in the
  flat domain — pcb owns this check because it gates fabrication and
  placement, before se ever sees a segment.
- Min bend radius from the stack (layer count / copper weight) as a
  material limit `mechanical_profile` exports per fold, so se DRC can
  check *declared install radius ≥ material min* — the same
  cross-boundary demand shape as catalog-derived demands.

**Projection extension** (contract already shaped for it):
`mechanical_profile` grows `segments` + `folds` keys; the se
derivation emits N segments instead of one. No re-shape — v1's
one-segment list was chosen for exactly this.

test: a two-segment flex dogfood board — fold DRC red when a tall part
sits in the bend zone, green after moving it; se shows two segment
sub-blocks joined by a hinge joint whose declared radius is checked
against the exported material min.

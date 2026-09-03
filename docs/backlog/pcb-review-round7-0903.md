---
status: open
title: "User visual review round 7 (2026-09-03): via keep-out under bodies, crystal rules honesty, web browse tab + schematic view, legend gutter"
prio: high
---

# User visual review round 7 (2026-09-03)

User reviewed the round-6 redesigned nano board ("It looks good") and asked
for the next wave. Verbatim asks, mapped:

## 1. Vias shoved out from under component bodies  — BUILD

"I'd like to add some pressure that the vias get shoved away from under U1
(it allows for easier pcb fixing even if it is a bit of a pain)."

Seam: `maze.py::OccupancyGrid.route` prices every layer change at a flat
`via_cost_mm` (module constant `VIA_COST_MM`, a routing *preference*).
Build: an optional per-cell **via surcharge mask** on the grid — cells under
any placed instance courtyard cost `via_cost_mm + surcharge` to change
layers in. Soft, not a keep-out: a via under a body must stay *possible*
(escape under a QFP is sometimes the only way), just priced. Realize builds
the mask from the same courtyard polygons placement used
(`instance_courtyard_polygon`), for ALL instances (a via under R1 is as
annoying to rework as under U1). Constant: `VIA_UNDER_BODY_COST_MM`,
starting at ~2x `VIA_COST_MM`; measure the reference boards' via counts
under bodies before/after.

## 2. Crystal layout rules — ANSWER + partial build

"Do we follow crystal setup rules perfectly ... does the llm tell router
and placer and do they understand?"

Honest state:
- `objectives.py` (objective vectors; has a crystal high-Z library entry)
  is **dark** — `annotation_for` has no production callers.
- `proximity` measures (precis-measures-help: "crystal hugs the MCU") are
  **evaluation-only**: `eyes.py` scores them for view='measures'; the
  anneal never sees them.
- Net classes carry route-time width/clearance rules only; no coupling or
  length terms.
- So: nothing today pulls Y1/C3/C4 to U1, shortens XTAL1/2 traces, or
  guards them from aggressors, beyond generic wirelength pull.

Slice worth building NOW (small): **measures drive the anneal** — a
`proximity`/`separation` measure authored on the design becomes an extra
placement cost term (piecewise-linear penalty past its goal), so the
LLM's stated intent is *enforced*, not just measured. This is the general
mechanism (same one covers "keep the switcher loop tight"), not a crystal
special case — consistent with objectives.py's own anti-special-casing
doctrine. Fixture: author `proximity` measures Y1<->U1, C3<->Y1, C4<->Y1
and net-class `clock` on XTAL1/XTAL2.
Full coupling-aware routing (objective vectors live) stays in
`pcb-engine-plan.md`.

## 3. Web browse tab + schematic — BUILD

"ready now to go into browse category in the server - and it should show
schematic too. And the svg nav block should be outside the board canvas
there."

- `routes/pcb.py` (analog: `mermaid.py`): `GET /pcb` list,
  `GET /pcb/{slug}` detail, `GET /pcb/{slug}/board.svg` (fab-level SVG via
  `PcbHandler.get(view='svg', level='fab')`), `GET /pcb/{slug}/schematic.svg`.
  Register in `app.py`; add `('pcb','PCB','/pcb')` to the base-template
  browse list.
- **Schematic renderer**: nothing exists (`easyeda.py` only *parses*
  schematic symbols). New `src/precis/pcb/schematic.py`: net-label style
  (each pin gets a short stub + net name; no drawn wires — auto wire
  routing of schematics is a research problem and the net-label style is
  the standard cheat), components as boxes with pin rows, power/ground
  nets rendered as rail/ground symbols. Pure function from the same
  design rows the board renderer reads. Exposed as handler
  `view='schematic'` so the MCP surface gets it too, web route reuses it.
- **Legend gutter**: `gerber_view.py` overlays the layer legend INSIDE
  the board viewBox (translate(4,4) over the canvas). Move it to a
  dedicated gutter column left of the board area (document width =
  gutter + board px), so it never covers copper — fixes every viewer,
  not just the web embed.

## 4. Unrouted pads re-observed (U2, R1, J4 nano; motor similar) — VERIFY

U2 pads = VBAT(8A)/5V(0.3A), J4 = VBAT/OUT1(2A): the known
current-annotated-net router failure (`pcb-engine-plan.md` "What board two
found" #1 / decision 3b) — do not re-file. **R1 = GPIO2/BASE1, signal-only
— if R1 truly has an unrouted pad it is a NEW failure mode**; render
utility now prints per-net unrouted findings (this round) — confirm from
the seed-2 output, root-cause if signal nets appear.

## Discovered while building (open defects, this round's residue)

- **Placement-chord phantom unroutes — FIXED this round** (pcb_route.py:
  `_residual_crossings` failed genuinely-routed nets off centroid-chord
  geometry; routed nets are now exempt, entries carry `reason`,
  regression tests pin it). Kept here only until ship.
- **Large rigid group strands off-board — OPEN.** An authored ~12x18mm
  group (U1 TQFP-32 + Y1/C3/C4 crystal circuit) ends with 25-31
  outline_containment errors at every tried seed/iters (2/2k, 2/8k,
  3/6k): the containment/edge pressure cannot walk a bloc that large
  back inside once it seeds straddling the outline — small groups
  (prog_hdr, 2 headers) are fine. Fix direction: containment-aware group
  seeding (seed the whole group inside the outline shrunk by the group's
  own envelope), or a coordinated group translate move proposed while
  containment pressure is nonzero. Until then the crystal circuit rides
  hard proximity measures (C3/C4 converge to Y1; Y1<->U1 ends ~20mm at
  nano seed 2 — congestion-limited, honest).
- **`5V` regulator net stays annotated 0.3A** — routes fine; only the 8A
  VBAT / 2A OUTn fail with `failed: width` (engine-plan 3b, known).
- **`pcb` slugs skip `mint_slug` sanitization** (`handlers/pcb.py` put:
  `slug = str(id).strip()`, any charset). Pre-ship review caught the two
  browser-facing XSS this enabled (fab SVG `<title>`/aria-label, schematic
  aria-label) — both fixed at the sink with regression tests. Residual
  design call: should pcb ids be charset-sanitized at put() time like
  other kinds? Defense-in-depth only now; low prio.

## Verify before delete-on-ship

Both boards re-rendered; user re-peek confirms via placement + web tab +
schematic legibility.

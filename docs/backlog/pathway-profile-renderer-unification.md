---
status: draft
title: One profile.js for the pathway energy diagram — precis page + autocatpath HTML export
---

# One profile.js for the pathway energy diagram — precis page + autocatpath HTML export

## Motivation / why
Three independent renderers draw the same reaction-energy profile: the
static matplotlib one (`autocatpath/viz.py::draw_profile` — PNG/SVG/PDF),
the HTML export's inline JS (`autocatpath/report.py`), and the precis
pathway page's inline JS (`precis_web/templates/refs/pathway_detail.html.j2`).
2026-08-16 brought them into *spec* agreement — one label-placement
contract (order-preserving PAVA stack, viz.py's `_stack_labels`, now ported
to both JS renderers) and one interaction contract (legend-as-buttons:
hover = emphasize, click = select + fade, Esc = clear) — but the two JS
implementations remain separate code that will drift again.

## In scope
- Extract a standalone, dependency-free `profile.js` renderer; home =
  autocatpath (the engine owns the semantics: TDTS/TDI, tiers, humps,
  supply edges), consumed by `report.py`'s export and vendored into
  `precis_web/static/` (same pattern as 3Dmol).
- Unify the data contract on the graph shape (`nodes` + `links` + `paths`
  — the precis `meta.graph` form; the export's per-path `levels/links`
  list is a lossy projection of it).
- Precis-only layers stay host-side, passed as options/hooks: the CHE
  potential lever transforms node energies *before* render; ghost overlay,
  measure traces, and fork-probability annotations are optional layers.
- Golden tests asserting identical label *orderings* between viz.py's
  Python `_stack_labels` and the JS port for shared fixture columns.

## Explicitly NOT in scope
- The static matplotlib renderer stays matplotlib (no headless-JS PDF
  machinery); it conforms by consuming the same canonical JSON and being
  the label-solver reference implementation.
- Pixel-identical output across surfaces — legibility parity, not
  screenshot parity.

## Acceptance criteria
- One JS file renders the profile on both the precis pathway page and the
  autocatpath HTML export; neither carries its own copy of hump/label/
  selection logic.
- Path selection (legend hover/click/Esc, drawing click) behaves
  identically on both surfaces.
- Label order top-to-bottom always matches bar order within a column on
  all three surfaces; leaders never cross (guaranteed by the PAVA stack).
- Golden ordering tests pass against viz.py fixtures.

## Target + blast radius
`precis_web/templates/refs/pathway_detail.html.j2` (shrinks to data prep +
hooks), `precis_web/static/` (new vendored asset + deploy role must ship
it), autocatpath `report.py` (export asset packaging), autocatpath release
cadence (precis pins a profile.js version — decide vendor-copy vs.
package-data import at spec time).

## Open questions / decisions log
- Vendor-copy into precis_web/static at bump time vs. serving from the
  installed autocatpath package's data files (keeps versions locked to the
  engine but couples web deploy to the engine venv)?
- Does the export's rank-based palette or precis' PATH_COLORS win? (One
  palette must — it is part of the shared marker/colour grammar.)

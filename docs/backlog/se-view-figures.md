---
status: draft
title: viz3d engine + view figures — cached stick/envelope renders of design objects in drafts
prio: high
model: opus
---

# viz3d engine + view figures

Design session 2026-09-14 (Reto + agent, jolly-cooking-haven worktree).
Companion: `se-nanobud-graph.md` (supplies the structures the first
figures render). First consumer: redraw dr173020's structural figures.

## Motivation / why

A draft figure of a 3D design object should be a *cached viewport*, not a
hand-made image: view params stored on the figure chunk, image regenerable
at higher refinement, stale-detectable by content hash. One render engine
for all 3D kinds (se atomic sticks, se envelopes, later cad) so every
improvement — occlusion, shading, scalebar — lands once. Today there is no
server-side atom→image path at all (`blocktree_svg` does envelopes only,
axis-aligned; `structure` is client-side 3Dmol).

## In scope

### 1. `src/precis/viz3d/` — pure, domain-neutral render engine (core)

Core placement is deliberate: `precis_se` imports core, never the
reverse; structure/cad figures need the same engine. Unit-agnostic like
the blocktree spine — coordinates + a unit label; units surface only in
the scalebar.

- **Camera**: `target` (default: bbox centroid of the selected content),
  `azimuth_deg`/`elevation_deg`/`twist_deg`, `projection: ortho|persp`
  (ortho default), `zoom` (ortho) / `distance`+`fov_deg` (persp).
  Az/el intent-style params, never matrices (LLM-settable).
- **Primitives**: ball, capsule/stick, convex hull/polygon, polyline,
  text label, scalebar.
- **Pipeline**: painter's depth sort (sticks split at midpoint for
  correct occlusion) → per-refine SVG emitter → raster via the existing
  resvg path (`figure_source._svg_to_png` precedent, zoom 3×).
- **Refine ladder** (render quality ONLY — geometry fidelity is the
  structure's relax rung, recorded there; one dial must not launder the
  other):
  - `r0` orthographic lines + dots, no sort (thumbnails, ms).
  - `r1` depth-sorted capsules, real radii, CPK colors, halo outlines.
  - `r2` gradient-shaded capsules/balls, depth fog (print target).
  - `r3` true raytrace — compute-lane job, deferred, same key scheme.
- **Lighting** (r2+): direction + ambient; `frame: camera` default
  (fixed key light under orbit), `world` override.
- **Scalebar**: ortho → exact, auto round value (~25% frame width),
  on by default; persp → omitted unless annotated at target depth.
  `style.scalebar: {auto}|{length}|false`.
- **Style**: `stick_radius`, `ball_scale`, `color: cpk|mono`, reserved
  `color_by` hook (per-atom scalar → colormap, for future computed
  properties; a value the renderer can't source renders as absent,
  never invented).

### 2. `view` figure medium

`chunks.meta.render` grows a second recipe kind beside `code`:

```json
{"kind": "view",
 "source": {"kind": "se", "slug": "...", "block": null},
 "view": "stick",
 "camera": {"azimuth_deg": 30, "elevation_deg": 15, "projection": "ortho",
            "target": null, "zoom": 1.0},
 "lighting": {"key_dir": [1, -1, 2], "ambient": 0.35, "frame": "camera"},
 "style": {"stick_radius_A": 0.12, "ball_scale": 0.25, "color": "cpk",
           "halo": true, "scalebar": {"auto": true}},
 "refine": 2,
 "cached_key": "<sha256>"}
```

- One branch in `utils/figure_source.py::resolve_figure_source`
  (blob-backed, like the `graph` medium) — export/reader/LaTeX/docx
  consume the cached PNG blob with zero downstream edits.
- `cached_key = sha256(structure_sha(scene) | tree content sha,
  canonical(view params), refine, VIZ3D_CODE_VERSION)` — reuses
  `structure.cache.structure_sha`; the run-cube idiom (append-only
  keys, roll code_version to retire). Source pins the **live slug**;
  the key records the geometry actually rendered; refresh is deliberate
  (quest-figure model), never reactive.
- Write path: existing `upsert_chunk_blob` + `stamp_render_key`.
  `derived-from` link figure-chunk → source ref (quest-figure precedent).
- **Progressive**: render `r1` synchronously on add/refresh; enqueue the
  target refine to the `figure_render` derived-lane job
  (`figure-kind-slices.md`) which overwrites the blob when done.
- Draft handler op to add/refresh a view figure; se-side assembly
  (`hydrate_bound_scenes`, poses, Å↔m at the existing seam) lives in
  `precis_se`.

### 3. View types

- `stick` — atoms/bonds of an se atomic design (or a bare `structure`
  passthrough). First.
- `envelope` — blocktree envelope projection as a figure (the unicycle
  case). Second user: this is the port of `blocktree_svg`'s projection
  onto viz3d — its three axis views become named cameras (top/front/
  side). Until it lands, two projection codepaths exist; accepted,
  named here so it doesn't fossilize.
- `scene3d` (figure-kind slice) later shares the camera vocabulary so a
  saved figure and the live viewer agree on what az/el mean.

### 4. Dev loop

`GET /structure/{slug}/stick.svg` + `GET /se/{slug}/stick.svg` web
routes (query params mirror the recipe) — instant eyeball loop for
tuning; not the figure path.

## Explicitly NOT in scope

- Reactive recompute on geometry change (staleness = key mismatch,
  refresh stays manual/op-driven).
- Rewriting `blocktree_svg.py` up front (second-user doctrine; §3).
- `r3` raytrace implementation (key scheme reserves it).
- Periodic/crystal scenes (fractional+cell; molecular scenes only v1).
- Faking computed color maps (binding energy, spin density) — `color_by`
  renders only sourced values; captions must say what a redraw shows.

## Acceptance criteria

- A stick view figure of an se atomic design lands in a draft, exports
  through LaTeX/docx unchanged (PNG blob), with scalebar + CPK at r2.
- Same recipe re-rendered → byte-identical key; geometry edit → key
  mismatch detected; refresh regenerates + restamps.
- r0/r1/r2 all render the same scene; r1-sync + enqueued-r2 upgrade
  overwrites the blob.
- Camera defaults frame an off-origin structure correctly (bbox target).
- dr173020 dogfood: at least the dc3015720-replacement panel rendered
  from a generated nanobud (needs `se-nanobud-graph.md` slice 2).

## Target + blast radius

New `src/precis/viz3d/`. Touches `utils/figure_source.py`,
`render/figure.py` (or sibling dispatcher), `handlers/draft.py` (op),
`precis_se` (view assembly), `precis_web` routes (dev loop),
`workers/job_types/` (figure_render lane). No migration
(`meta.render` is JSONB).

## Open questions / decisions log

- Decided: engine home `precis/viz3d/` (core, not `precis_se`) — cad/
  structure/pcb figures share it; se-viz would re-create the plugin
  placement problem.
- Decided: refine = render quality only.
- Decided: live-slug source + deliberate refresh.
- Open: draft handler op name/shape (`add_view_figure` vs a mode on
  `_add_figure`).
- Open: does `envelope` land in this item or as follow-on once stick
  ships (leaning follow-on).

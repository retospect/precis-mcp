---
status: draft
title: "surface-Pourbaix: close out the precis half of mixed NO*+H* coverage scans (post-deploy check, engine-version key, real-model run)"
pillar: quests
prio: normal
---

# Mixed NO*+H* coverage: what precis still owes

Source: gr489475 (stays OPEN until the post-deploy check below passes; this
item is its precis-side remainder, not a closure). Engine half: catpath
`coverage.mixtures` (0.27.0, PR #3); catpath is a separate repo.

## Already landed (do not redo)

- Request: `coverage_job.with_defaults(config, mixtures=runner.coverage_mixtures_supported())`
  adds `[{species:[<feed>,"H"], ratios:[[3,1],[1,1],[1,3]], coverages:[0.5,0.75]}]`
  when the substrate has a feed reservoir; caller-set mixtures pass
  verbatim; an older engine gets the key stripped and the job_event says
  "mixtures dropped". `surface_anchors._proposal_scan_params` unions
  proposal-named mixtures (83b1bf24f, gr489475 comment 2).
- Read: `surface_pourbaix.anchors_from_coverage` parses `facets[].mixtures[]`
  into anchors with per-species counts (slope `-Σ nᵢνᵢ/area`, key
  `(label, counts)`); winners, boundaries and `anchor_spread` carry counts.
- `pool_anchors` keeps reservoirs (fix found on the way).
- Engine pin `autocatpath>=0.29.0` and `catpath` rev v0.29.0 are in
  `pyproject.toml` on main; gr489475 comment 4: r42 2120eeb90 carries pin
  plus wiring.

## What is left

1. **Post-deploy acceptance check** (gr489475 comment 3, 2026-10-16 12:54Z,
   quoted): "the surface_coverage_scan job_event reads 'mixtures=NO+H',
   coverage.json has facets[].mixtures[].points, and the map shows a mixed
   termination or names why none wins." Run on qu164903 / st261823 (3x3,
   `n_top` 9, so 3:1 and 1:3 at 0.5 ML are representable) after a release
   that carries the engine pin. Not run as of comment 4 (2026-10-16 15:15Z).
   `coverage_anchor_key` hashes the engine version, so the next dispatch gets
   a fresh key and a fresh scan.
2. **Version-key mismatch guard** (gr489475 comments 3-4, "not seen on
   prod"): if the dispatch host and the compute host run different engine
   versions, the scan keys under the dispatcher's version while the worker
   drops mixtures, and that key never re-scans. Comment 3 declined a
   speculative fix (deploys install one wheelhouse fleet-wide). Reto
   decides: build the guard (worker reports `coverage_mixtures_supported()`
   in the job_event and the harvest refuses a mixtures-requested scan whose
   worker dropped them), or wait for an observed case.
3. **Real-MLIP NO+H grid** (gr489475 comment 2: "a real-MLIP NO+H scan on an
   n_top >= 8 slab (cluster)"): the wiring is tested on fixtures and fakes
   only. A real scan needs a slab with `n_top >= 8` (a 4x2, or st261823's
   3x3); on a smaller cell the 3:1 / 1:3 splits drop at 0.5 ML and the engine
   warns. It is cluster compute: Reto decides when and on which model
   (medium, large), within the standing <$25 per action.
4. **Engine top-level winner/mari labels** (gr489475 comment 1, item 3:
   "coverage.json winner/mari may now be a mixed label with counts; precis does
   not read those today"). Checked 2026-10-16: no `mari` read in `src/precis*`;
   the `winner` fields in `surface_pourbaix.py` and `surface_anchors.py` are
   precis's own sweep output. Nothing to build; recorded so it is not
   re-investigated. Build only if a consumer of the engine's own
   `winner`/`mari` appears.

## Explicitly NOT in scope

- Summing single-species γ to approximate a mixed phase (invented physics;
  gr489475 body).
- Catpath changes: push, version bump, ordering search (v1 limit is one
  max-min arrangement per count vector).
- The spectator-layer mode: `docs/backlog/crowded-surface-experiment.md`.

## Acceptance criteria

- Item 1 passes and is recorded on gr489475 with the job id; only then is
  gr489475 closed.
- If item 2 is built: a test where the worker reports no mixtures support
  and the scan is refused or re-keyed rather than silently pooled.

## Open questions / decisions log

- Reto decides: item 2 (guard now or wait).
- Reto decides: item 3 (when, which model).

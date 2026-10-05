---
status: in-progress
pillar: platform
title: perspective SVG pixels per unit at the focus plane
prio: medium
---

# gr462672 — confirmed bounded visualization repair

Native open/unclaimed; claimed wip. Native render_svg symbol matches local
render shape, served corpus/container root differs from the explicit source
worktree; all edits use local root. Perspective camera.project correctly
returns dimensionless f/depth. Renderer applies px_per_unit directly,
shrinking figures; projected radii share the defect. Rescale renderer-only
xy by distance/f and radii by distance/depth, keeping project() unchanged.
Pixels per scene unit is exact at target depth, retaining near/far perspective.
Ortho and camera.zoom behavior remain unchanged. Re-enable perspective
scalebar with explicit '(at target depth)' label and world-unit pixel length;
False still disables. No geometry/scientific/scene/camera API change.
Synthetic render regression all refine rungs, two distances/FOVs, known target
plane ball/line positions/radii, near/far depth ratios and qualified scalebar.
Coordinator owns version/full gate/deploy.

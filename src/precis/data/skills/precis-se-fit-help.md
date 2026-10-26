---
id: precis-se-fit-help
family: se
title: precis — the se fit check (view='fit': interference, gaps, mass, build diff)
summary: view='fit' runs the mesh fit check on a design's placed parts — real cad geometry, not envelopes — reporting connect-rule violations (press/bearing/screw/snap gaps and overlaps), unconnected interference, mass, and a new/cleared/changed diff against the previous import's stored report
answers:
  - how do I check my design for interferences and wrong fits?
  - why does view='fit' sometimes serve a stored report, and how do I force a recompute?
  - how does the build-over-build diff (new/cleared/changed) work?
applies-to: get (kind='se', view='fit'), workspace imports (manifest fit_rules/fit keys)
status: active
tags: design
kinds: se
---

# precis-se-fit-help — the fit check

`get(kind='se', id='<design>', view='fit')` tessellates every placed part
(manifold3d, ~0.2 ms per pair) and measures every pair whose inflated
AABBs can touch plus every connected pair. Geometry is the bound cad
design — an envelope-only block is checked on its posed envelope and the
header says `envelope×N`; a block with neither cad nor a usable envelope
is listed "not fit-checked". Authoring views live in [[precis-se-help]];
other check views in [[precis-se-check-help]].

## What the table reports

`severity | pair | gap / overlap | expected | rule | made-by a | made-by
b` — made-by is the script's `file:line` for each part. A connect's
expected fit comes from its `joint.mechanism` (else `joint.class`):
`press` expects gap ≤ 0 (overlap allowed — an interference fit; a
positive gap beyond tolerance is the finding),
`bearing`/`key`/`clamp`/`snap`/`rigid` expect contact (gap ≈ 0; overlap
thicker than tolerance is the finding, reported as volume + thickness),
`screw` is contact plus a thread-depth overlap allowance,
`prismatic`/`cylindrical`/`revolute` expect a positive gap, `integral`/`bond`/`cable`/`tensioner`/`magnet`
are listed "not fit-checked". The strictest rule wins on multi-connect
pairs and names every connect's endpoints. Unconnected pairs that touch
or overlap are findings; a result inside the mesh's chord band is
re-measured at chord/10 (`[refined]`). The `# mass` section weighs parts
that carry a `material` line (missing/unknown materials refuse the mass
line, not the report). `args={'recompute': true}` forces the live pass;
past the ~200 ms live budget the view serves the last import's stored
report labelled with its build and age.

## Import and the diff

A workspace import recomputes the check and stores its own report at
`meta.workspaces[<ws>].fit` — a manifest `fit` payload is ignored input,
never trusted (Decision 9). `fit_rules.min_clearance` pins a per-pair
expectation: `[{'pair': ['a', 'b'], 'value': '2 mm'}]`. The `# diff`
section lists `new:`/`cleared:`/`changed:` findings vs the previous
import (keyed by rule + pair, never by value) plus the mass delta. The
script-side twin is `out.check()` — run at `finish()` by default —
whose `out/fit.json` is the offline copy of the same table.

---
status: draft
title: cad straddling modules — port payload geometry (a hinge that adds AND cuts in both hosts)
prio: medium
model: opus
---

# cad: straddling modules (host-modifying features)

Design session 2026-09-04 (Reto + agent), imperative-plotting-hare worktree.
Reto: mech blocks "can be defined with modules that are… not aligned (a
hinge module that… enters the two connected blocks partially)."
Best-practice grounding: `perplexity-research:317035` — the *Bidirectional
Cross-Part Feature Dependency Pattern*; canonical industrial example is the
SolidWorks Hole Series (an assembly feature whose holes live in the parts
as externally-referenced features).

## The shape — payload geometry on ports

A module's port may carry **payload geometry**: `add` payload (the hinge
barrel it brings) and `cut` payload (the knuckle recess, the pin bore) that
mate-expansion **splices into whichever component the port mated to**. The
mate already knows both bodies (cad slice 2); expansion stays a pure
source-to-source transform; the DAG stays legible; no new persistence shape
beyond the port meta (`spec.meta['ports']` grows a payload field).

Sketch (syntax to be settled at build time):

```
component hinge
barrel  add  cyl:r4h20
port leaf_a @-10,0,0  cut:box:w8d3h20      # recess cut into the mated host
port leaf_b @10,0,0   cut:box:w8d3h20
pin_bore    port:leaf_a cut cyl:r2h24 @0,0,-2   # or payload lines addressed to a port
```

## Honesty rules (from slice-2 posture + the report's failure modes)

- **Attribution crosses boundaries loudly.** The hinge's cut payload
  removes material from `bracket_a`; mass/volume probes must report
  "hinge contributes −0.8 cm³ to bracket_a", never silently mutate the
  host's numbers. Same rule at Å scale: a bond-forming interface consumes
  atoms/valences of both blocks — the nm face-code layer is the eventual
  second consumer of the same splice semantics.
- **No orphaned payloads.** A port with payload that is never mated is a
  lint warning (the geometry exists in no host); a payload cut applied to
  a host it fully destroys is the empty-component error, already linted.
- **Derivation is recorded, drift is loud.** The spliced cut belongs to the
  module — re-expansion from source re-derives it every read, which dodges
  the classic "underived hole" drift *except at export*: exports record the
  source version and go stale via the `attached-models-layer.md` watcher.

## Sequencing

- This is **cad slice 5** (after joints + sweep): pure kernel + parser, no
  migration. Joints first, because a hinge module wants to declare its
  joint (`revolute` about the pin axis) in the same breath as its payloads —
  designing payloads before joint syntax exists would guess the grammar.
- Refusals to carry over from slice 2: patterned hosts need explicit
  handling (a payload into a `polar:n4` array must splice into every copy
  or refuse — decide at build).

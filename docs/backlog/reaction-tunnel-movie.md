---
status: draft
title: a four-pane reaction movie shows a substrate walking the tunnel — atoms, fields, frontier orbitals, strain — on one camera with an energy strip
pillar: 3d-design
prio: low
blocked-by: reaction-tunnel-station-path-finder
---

# Reaction movie (T6 of the reaction-tunnel chain)

Umbrella: [no-nh3-reaction-tunnel](no-nh3-reaction-tunnel.md). Reto,
2026-10-04: the outcome of the chain is a video of the reaction as it
happens in the tunnel, once the fields and poses are done.

## Motivation / why

The design is a stack of co-registered fields along one axis. A split-
screen movie that keeps them registered is how a human checks the stack
is coherent: a bend, a pore, an orbital overlap and a barrier should line
up on screen. It is also the deliverable shown to people.

## In scope

**Timeline.** The reaction coordinate, not clock time: the concatenated
minimum-energy paths from T1/T5 station records, from each station's
reactant through its TS to its product, joined by linker runs. In real
time the film would be almost all waiting at the highest barrier, so the
frame says "minimum-energy path, not real time" and shows the real time
on the dwell bar (below). Thermal motion only from real ensemble
conformers; no invented wobble.

**Layout.** A 2×2 square plus a full-width strip under it, close to 16:9.
- **One rule: all four panes share one camera and one z-window**, so a
  single scale bar is true everywhere and the same screen spot is the
  same place in the tunnel.
- Top-left, ball-and-stick: the substrate walking; lining pendants; carbon
  cut away (front half removed or ghosted); the inlet pore lit when it
  delivers H. Corner: station k/N, H delivered so far (0–5).
- Top-right, fields: electrostatic potential on the r(z, θ) lining
  surface; H-bond contacts dashed. Corner: colour-bar range, substrate net
  charge. Alternative mode: the cylinder unrolled flat (z across, θ down),
  with pores and anchors as fixed marks and the substrate as a moving dot.
- Bottom-left, frontier orbitals: substrate HOMO/LUMO isosurfaces plus the
  nearest pendant's lobes. Corner: HOMO–LUMO gap in eV, the reacting
  orbital pair.
- Bottom-right, strain: atoms coloured by strain energy, the N–O bond as a
  gauge. Corner: N···O distance, stored strain in kJ/mol, and whether
  strain is per-atom MLIP energy or bond deviation.
- Centre badge: formula chain with the current species lit, layer/ring
  index, scale bar.
- Strip: free-energy profile along the tunnel with a moving cursor, each
  Ea labelled, uphill steps marked. Below it a log-scale dwell bar
  (Eyring rate per station).

**Outputs.** An MP4, and a web scrubber with the same panes and a slider
along the reaction coordinate. The video is shown; the scrubber is the
design tool. The scrubber reuses the se 3D viewer where it can.

**Rendering.**
- Draft frames and all 2D overlays (badge, corners, strip): `viz3d`, which
  has stick figures, camera and scale bar. It needs a mesh primitive and
  marching cubes over cube files for isosurfaces.
- Final 3D panes: an offline ray-tracer (OVITO Python or Blender; pick in
  review). ffmpeg composites.
- Checking: pixel-diff frames against reference frames, as the se 3D
  viewer does.

## Explicitly NOT in scope

- Computing anything. Every frame's data comes from T1/T5 station
  records; a missing cube is an error, not a computation.
- Molecular dynamics, or a real-time film.
- Audio or narration.

## Acceptance criteria

- From one T1 run with at least two stations, a single command renders
  the MP4 and the scrubber with no new computation.
- A test frame proves the shared camera: a fixed marker at known (z, θ)
  lands on the same pixel in all four panes.
- Orbital sign stays consistent across frames: no colour flips at a
  constant species.
- Every pane's corner labels its method rung (MLIP/xTB/DFT).
- Pixel-diff reference frames for one station are checked in as test
  fixtures.

## Target + blast radius

`src/precis/viz3d` (mesh primitive, isosurface, frame-sequence export).
A movie job in the pathway pack, output as a `figure` or file artifact.
Web: a scrubber page in `precis_web`. New optional dependencies: ffmpeg
in the worker image, and the ray-tracer if chosen.

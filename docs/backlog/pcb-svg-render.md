---
status: draft
title: SVG rendering of boards and IR levels (screen + publication quality)
prio: high
model: opus
---

# SVG rendering — boards and IR levels

Verified 2026-08-28: **there is no PCB rendering of any kind.** The only
SVG in the tree is pdf.js iconography and web assets. `pcb/gerber.py`
writes Gerber X2 + Excellon text and nothing else. There is also no real
board yet — only synthetic test fixtures — so "look at the board" is
currently impossible by two independent routes.

## Architecture: render from the MODEL, not from the Gerber

SVG is a **sibling export of Gerber from the same copper model**, not a
downstream parse of Gerber output. The model already holds everything
needed: tracks as line/arc segments, pours as polygons, pads, vias, silk
strokes, board outline.

Parsing Gerber back to make pictures would be wasteful *and* circular — the
renderer would faithfully reproduce the writer's bugs and independently
verify nothing. Same source, two derived outputs; the existing
sketch-as-canonical discipline applied one level further out.

## Publication-quality requirements

- **True vector, never rasterized** — scales in LaTeX at any figure size.
- **Greyscale- and colorblind-safe layers.** Colour-only layer distinction
  fails in print and for a substantial fraction of readers. Layers need
  hatching or stroke style *in addition to* hue.
- **Deterministic element ordering** so a regenerated figure diffs cleanly.
  The paper's §2.4 argument is about checkable results; irreproducible
  figures undercut it.
- **Renderable subsets** — single layer, copper-only, ratsnest-only, silk
  only — for multi-panel figures.
- Scale bar, mm units, configurable palette.

## The money figure: render intermediate IR levels

For a paper about *topological* place-and-route, the compelling image is
the **rubber-band sketch (L2/L3) beside its realized copper (L5)**. Almost
no other tool can produce it, because almost no other tool keeps topology
as a first-class object — most derive it from coordinates and throw it
away.

A finished-board photo looks like every other PCB paper. Sketch-and-
realization looks like this one. Prioritize L2/L3 rendering at least as
highly as L5.

## Also worth having

Screen rendering in `precis_web` — being able to *look* at a board during
design has practical value well beyond figures, and would have surfaced
several of this build's silent geometry bugs far earlier than the oracle
did.

## Sequencing

**Independent of the via-geometry work** — this reads the model, it does
not change the realizer. Can run in parallel; touches a new module plus a
handler view rather than `realize.py`/`cost.py`/`drc.py`.

Note it will render vias only once via geometry exists — until then the
pictures are as via-free as the boards.

> **Consider for the paper.** The sketch-vs-realization figure is a
> contribution-carrying image, not decoration. See
> `pcb-paper-benchmark-selection.md` §CONSIDER FOR THE PAPER.

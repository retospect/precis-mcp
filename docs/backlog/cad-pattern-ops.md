---
status: draft
title: "cad DSL: pattern ops (mirror / rotate / tile) as one motif mechanism"
pillar: 3d-design
---

# cad DSL: pattern ops

Source gripes: gr536408.

## Motivation / why

Discussion 2026-10-29 (box3 context): the DSL has no mirror/rotate/tile;
symmetry is generator-side data tables (box3's CORNERS loop), which is
right for small counts but scales badly for pin grids, bolt circles and
fin arrays. The SDF representation makes the math nearly free — all
three are coordinate rewrites on the query point before subtree
evaluation (mirror = abs on one axis; rotate = inverse rotation; tile =
mod into canonical cell); the field stays exact, Lipschitz preserved,
and both export routes are unaffected (the field route marches the
composed field; the manifold route instantiates per-copy).

## In scope

If the mechanism is ruled worth it (the whole item hinges on the open
question below):

- DSL grouping syntax — a pattern needs a named motif of components plus
  an instance op.
- A traceability contract — expand instances in payloads (viewer rows,
  3MF parts, sink reports want addressable instances) while instances
  share the motif's source FeatureRef, which strengthens rather than
  weakens stacktrace attribution.
- Tests, including watertightness on mirrored designs.

The gripe's estimate: 2–4 days for the shared mechanism, forward-only
(new op types, no migration).

## Explicitly NOT in scope

- Changing or removing the Python-generator pattern path — it stays
  either way.
- Swept or moving patterns; only static mirror / rotate / tile are in
  the ask.

## Acceptance criteria

TBD — hinges on the ruling. If approved:

- A motif repeated by each of mirror / rotate / tile exports a
  watertight design, and instances are addressable in payloads.
- Instances share the motif's source FeatureRef for stacktrace
  attribution.

## Target + blast radius

cad DSL grammar and fold (`src/precis/cad/`), both export routes, and
every payload that lists per-instance rows (viewer, 3MF parts, sink
reports).

## Open questions / decisions log

- **Open — Reto to decide (the gripe's own recommendation):** is a
  model-side pattern/motif mechanism worth the DSL-grouping and
  traceability-contract work, or is the Python generator already a good
  enough pattern language (per-instance tree naming is an inspection
  feature, not a modelling one)? Model-side patterns must beat the
  generator ergonomically.
- Known pitfall to design around: mirrored/tiled copies touching at
  shared planes create coincident-face unions (the fold's shallow-read
  band, documented in `inspect.py`) — fine for spaced bosses, needs
  discipline for shared-wall tilings.

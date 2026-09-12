---
status: ready
title: box full-dims at the DSL/MCP surface — half-extent stays kernel-internal (gr334785)
prio: high
model: sonnet
---

# Box full-dims cutover (gr334785)

Delivers the DECIDED gr334785 ruling (its comment 1): the DSL/MCP
surface reads box `w/d/h` as FULL dimensions — what a human or LLM
means by "a 40 mm wide box" — dividing by two at the boundary into the
kernel's half-extent primitive, which stays internal and unchanged.
Lands in the same window as `units-policy-cutover`, back-to-back
(both rewrite the same `cad/dsl.py` boundary); carved out of that spec
so it has its own acceptance criteria.

## In scope

- DSL grammar + se/nm/cad op boundary: box dims are full extents;
  kernel primitive keeps half-extents; exactly one ÷2, at the boundary.
- Data migration for envelopes authored under the half-extent reading
  (~24 on unicycle-printed-v1; sweep all live se/nm designs for
  box-typed envelopes and convert), forward-only, with a pre/post
  bounding-box checksum proving geometry is unchanged.
- Docstrings/skills that state the convention.

## Explicitly NOT in scope

- Any other primitive's parameter convention (sphere r, frustum radii
  unchanged); the units grammar itself (owned by units-policy-cutover).

## Acceptance criteria

- `box w=40mm` yields a solid measuring 40 mm across (probe_ray
  through it returns 40 mm chord), pre- and post-migration designs
  agree.
- Migrated designs revalidate with numerically unchanged validate/DRC
  output; checksum test in the migration's test.
- Kernel half-extent internals show NO change (no diff under
  src/precis/cad/ primitives beyond the boundary conversion site).
- gr334785 closeable on ship.

## Target + blast radius

`precis/cad/dsl.py` boundary · one se + one nm data migration ·
skills/docstrings stating full-dims. Post-deploy: unicycle +
photonic-arm envelopes render identically.

---
status: draft
title: cad print-in-place joints — built-in grooves/pins when both sides share a print
prio: medium
model: opus
---

# cad: print-in-place joints

Reto, 2026-09-05: "for say the 3d printed kinds, we can have xy
constraints, slidy grooves, and rotateable pins all built in! If both
sides are printed."

## Already expressible — the new part is honesty

Slice 5 payloads + slice 3 joints already express a print-in-place
hinge: a library module whose port carries the pin as an `add` payload
into one host, the bore as a `cut` payload (pin radius + clearance) into
the other, plus the `joint … revolute` line. A slidy groove is the same
pattern with `prismatic` and a rail/groove payload pair. What this item
adds:

1. **Seed library** (stored cad refs, reused via `use`, each with a
   worked-example test): `printed-hinge` (rotatable pin), `printed-slide`
   (dovetail/groove prismatic), `printed-pin` (snap pivot). Sweep validates
   their travel out of the box.
2. **Process clearances as one-sided constraints** — the clearance
   belongs to the *fabrication process*, not the design: FDM pin gap
   `>= 0.3`, SLA less, milled pairs use real fit classes. Expressed with
   `dim`/`constrain` (`cad-dims-and-constraints.md`) so an undersized
   printed joint is refused, not discovered on the build plate. Build
   AFTER the dims kernel.
3. **The "both sides printed" precondition is checkable** — a
   print-in-place joint is realizable iff both host components are
   `made-by` the *same print step* (two components in separate print
   jobs cannot share a captive pin). One-query lint riding the make-tree
   coverage-lint slot (`make-tree-vs-design-tree.md` v1, shipped) — the
   first real consumer of step-level alignment. Realization-mode
   awareness proper (which joints are printable vs need hardware) can now
   build on the `realized-by` edge (shipped, mig 0156).

## Status

Shipped 2026-09-05 (13187799, with the dims kernel de851f3f preceding):
the `printed-` port-type convention, the same-print-step lint on the
design's `view='links'` (fires when both hosts of a printed-typed mate are
`made-by` different print steps; silent without alignment info), and the
worked printed-hinge example (module-owned pin + clearance-bore payload,
clearance floor as a `dim`). Open below.

## Remaining

- **Seed library as stored refs** (`printed-hinge`, `printed-slide`, `printed-pin`
  as prod cad designs reusable via `use`) — a deliberate prod-write
  session, not an autonomous one.
- **Process-clearance profiles** (per-process floors: FDM 0.3, SLA less,
  milled = fit classes) — the consumer that ties a joint's clearance dim
  to its realization process. The `realized-by` edge it wanted shipped
  2026-09-05 (mig 0156, catalog-parts slice — `cad-machine-spec.md`
  §Parallel track), so this is now unblocked.

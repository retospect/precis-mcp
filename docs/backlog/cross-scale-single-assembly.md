---
status: draft
title: Co-design a PCB, a microfluidic cartridge stack and a motor as ONE se assembly
prio: high
blocked-by: pcb-se-binding
---

# Co-design a PCB, a cartridge stack and a motor as one se assembly

Pillar: 3d-design

Reto: co-design macro "laminar objects" (PCB, microfluidic cartridge) with
motors/mechanics as ONE assembly with the nanoscale designs — not three
separate artifacts stitched together after the fact.

Evidence: peer session EWOD, 2026-09-30, plus `pcb-se-binding.md` (already
in flight, `blocked-by` here). pcb already emits `export.mechanical_profile`
(`src/precis/pcb/export.py` — `pcb-se-binding.md` calls it "the 0041
bridge", mm-native, `×1e-3` at the one crossing), but its only caller today
is pcb's own `view='mechanical'` export builder
(`src/precis/handlers/pcb.py:1826`) — no cad/se consumer reads it yet
(that consumer side is exactly what `pcb-se-binding.md` builds).
`se.set_binding` enumerates `cad|structure|component|part`
(`src/precis_se/ops.py::_BINDING_KINDS`) and, before `pcb-se-binding.md`
ships, cannot name a board at all. HV supply is procurement-only today
(`ewod-controller-and-hv-supply.md`). The cartridge's own mechanical stack
has no representation — `gr451662` (verified 2026-09-30, open): the EWOD
generator emits only gerbers, nothing for the ITO top sheet, spacer
adhesive, or alignment features a laser cutter/Cricut needs, and the
gripe's own "why it matters" names exactly this drift risk (mechanical
layers desyncing from the electrode layout they must register against).

Microfluidic cards were PARKED by Reto 2026-09-12
(`multiscale-design-architecture.md:83`, `multiscale-design-system-spec.md`
§4.8) and are **UNPARKED by this review, 2026-09-30, to the horizon only** —
this item names the target, it does not authorize building past
`pcb-se-binding.md`'s prerequisite.

## Motivation / why

`pcb-se-binding.md` solves one board bound into one se design. The actual
design target is broader: a board, a microfluidic cartridge stack (ITO
sheet, spacer adhesive, alignment features — `gr451662`'s mechanical-layer
gap), and a motor as children of *one* se design under *one* contract set,
with one mm→m crossing (Reto ruled 2026-09-14, restated in
`pcb-se-binding.md`'s units ruling) — not three designs manually kept in
sync by a human.

## In scope

- A PCB (via `pcb-se-binding.md`'s binding), a cartridge mechanical stack,
  and a motor, each bound as children of one se design.
- One mm→m crossing for the whole assembly — reuse `pcb-se-binding.md`'s
  single-seam ruling rather than adding a second conversion site per
  laminar object.
- The cartridge stack's mechanical representation: at minimum the layer
  set `gr451662` names (ITO cut layer, spacer adhesive with bridge/kerf/
  mirror handling, alignment features shared across layers) as se-visible
  geometry, keyed to the same board coordinates the PCB uses.
- Motor mounting as an ordinary se block with a joint to the assembly —
  no new joint kind, reuses `cad-machine-spec.md`'s existing vocabulary.

## Explicitly NOT in scope

- Building `pcb-se-binding.md` itself — hard blocker, this item starts
  after it ships.
- The cartridge layer generator internals (bridge placement, kerf offset,
  SVG export mechanics) — `gr451662` owns that; this item consumes the
  resulting mechanical layer as se-visible geometry, it does not build the
  generator.
- HV supply design — stays procurement-only per `ewod-controller-and-hv-supply.md`
  until that item's own scope changes.
- Two-way sync between any of the three objects — `pcb-se-binding.md`'s
  "owned data, message-passing, never two writers of one field" rule
  applies unchanged to the widened three-object case.

## Acceptance criteria

- One se design binds a PCB, a cartridge mechanical stack, and a motor
  simultaneously, each visible in the same world-pose space.
- Exactly one mm→m crossing site for the whole assembly (test-pinned, same
  discipline `pcb-se-binding.md` specs for the PCB-only case).
- The cartridge's alignment features register consistently against the
  PCB's own coordinate frame (the drift `gr451662` warns about is checkable,
  not just hoped-away).

## Target + blast radius

`precis_se` ops/persist/validate (extends `pcb-se-binding.md`'s binding
machinery to the cartridge + motor case); `src/precis/pcb/` (cartridge
mechanical-layer export, shared with `gr451662`'s fix); no new joint
machinery expected for the motor (reuses `cad-machine-spec.md`).

## Open questions / decisions log

- Whether the cartridge stack binds as a second `bound_kind` value
  alongside `pcb`, or as an extension of the pcb binding itself (one
  board may own both its copper and its cartridge overlay) — undecided,
  depends on how `gr451662`'s generator work shapes the cartridge's own
  data model.

Closest existing items: `pcb-se-binding.md` (hard blocker — this item is
the widened N-object case), `cad-machine-spec.md` (motor joint vocabulary),
`multiscale-design-system-spec.md` §4.8/§6.2 (the microfluidic-card design
this item unparks), `ewod-controller-and-hv-supply.md`, `gr451662`. Thread:
`docs/backlog/threads/se-machine-design.md`.

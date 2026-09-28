---
status: draft
title: A stackup edit can orphan a net-class layer lock, and says nothing until route time
prio: low
---

# A stackup edit can orphan a net-class layer lock

## Motivation / why

`put(args={'op':'stackup'})` (shipped 2026-09-28) refuses to strand
COPPER — a layer carrying tracks, vias or a plane assignment cannot be
dropped. It does not check net-class `rules["layers"]` locks. So a class
locked to `In2.Cu` survives a stackup that no longer has an `In2.Cu`, or
that demotes it to `role: plane`.

This is a UX gap, not a correctness bug: it fails legibly at route time as
`UnroutedReason(kind="layer_lock")`, with `_net_class_layers` having found
no usable layer, and that message already names the class and the layer.
Nothing silently mis-routes.

Worth filing anyway, because the whole point of the stackup op is that a
declaration the engine quietly drops is the defect it exists to stop
producing — and "your lock now refers to nothing" is a declaration in that
same family. The stranding refusal already sets the precedent for catching
it at authoring time.

## In scope

Check `pcb_net_classes.rules["layers"]` in `handlers/pcb.py::_op_stackup`,
alongside the existing copper-stranding check, and report per class:

- a locked layer absent from the new stackup;
- a locked layer present but no longer routable
  (`ir.layer_is_routable` false for it).

**Refuse, or warn?** The copper check REFUSES because dropping the layer
would leave unresolvable rows. An orphaned lock leaves nothing
unresolvable — it just guarantees a later failure. A warning in the
response body may be the better shape here; a refusal would block the
legitimate "open In2.Cu, then fix the class" two-step, which is exactly
the workflow the skill documents as "a stackup is half the change".

## Explicitly NOT in scope

- The copper-stranding check itself. It is correct and tested.
- `UnroutedReason(kind="layer_lock")`'s message. It is already precise;
  this item is about saying it EARLIER, not saying it better.
- Auto-repairing the class. Rewriting a net class because a stackup edit
  made it awkward is the engine deciding design data, which this campaign's
  governing constraint forbids.

## Acceptance criteria

- A board with a class locked to `In2.Cu`, given a stackup without
  `In2.Cu`, surfaces that at `op='stackup'` time naming the class and the
  layer.
- A board with a class locked to `In2.Cu`, given a stackup that keeps it as
  `role: plane`, surfaces it too — the routable predicate, not just
  presence.
- The legitimate two-step (open a layer, then point a class at it) still
  works in either order.

## Target + blast radius

`src/precis/handlers/pcb.py::_op_stackup` only, plus
`src/precis/data/skills/precis-pcb-route-help.md` if the answer is a
warning the caller should expect to read.

## Open questions / decisions log

- **OPEN — refuse or warn?** See "In scope". Leaning warn, for the
  two-step reason.

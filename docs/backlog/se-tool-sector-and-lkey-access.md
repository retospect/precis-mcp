---
status: idea
pillar: 3d-design
title: se tool access — model a ratchet sector / L-key swept volume instead of a full turn
---

# se: ratchet-sector and L-key tool access — the swept volume a handle does not need

`src/precis_se/toolaccess.py` models one tool as the swept volume of a
**full turn**: a shaft cylinder plus an arm disc of `swing_radius`. For a
coaxial tool that is exact — a screwdriver spun in place sweeps a body of
revolution, so there is nothing to approximate. For an L-key or a wrench it
is pessimistic: a handle that only needs a *sector* to ratchet back and
forth can work in a joint the full-circle model refuses.

The module names this in its own deferred list: "ratchet arc, i.e. a handle
that only needs a *sector* rather than a full circle — a real escape for a
tight joint, and the reason this check warns rather than refuses." The
warn-not-refuse behaviour is the current mitigation, and it is why a real
design can carry a `no_tool_access` finding that a machinist would shrug at.

This item was split out of `se-mechanical-drc.md` because that file's
rulings deferred it twice without giving it a home:

- **Ruling 2** scopes the combined hand+tool volume to screwdrivers and
  electric screwdrivers, and states that for coaxial tools the "full 360°
  sweep versus ratchet sector arc" question does not arise — "that
  distinction only returns for L-keys and wrenches."
- **Ruling 3** keeps ratchet arc and angle-of-approach on the excluded list
  for the hand work, alongside the docstring exclusion it overturns.
- **Ruling 7** (2026-09-30) confirms the existing hex-key envelopes stay as
  they are, on the no-hand full-circle treatment, while the hand is added
  for screwdrivers first.

So after `se-mechanical-drc.md` ships, L-keys and wrenches are the one tool
class still modelled by a volume nobody believes — correctly conservative,
but conservative in a way that costs real designs.

## Open design questions — do not answer, just sketch

(a) What declares the available sector? A shop-level "minimum usable arc"
constant, a per-tool-class field in `precis/data/driver_envelopes.json`, or
a per-joint annotation by the designer?

(b) A sector sweep is orientation-dependent in a way a disc is not — the
check has to try arcs at some angular resolution, or solve for the best
arc. That is a cost question against the ~2.3 s `cad.relate.clearance` call
the module already budgets hard against.

(c) Does a sector-limited pass belong as a *third* verdict ("fits with a
ratchet, arc X available") rather than a second pass of the same
yes/no? The module's stated value is naming WHICH tool — "long-arm hex key
only" is an instruction, "no access" alone is an argument — and an arc
readout is the same kind of instruction.

## Blocked by

`se-mechanical-drc.md` — the hand+tool volume model lands there first, and
ruling 2 intends the same per-tool-class volume model to accommodate these
later. Building the sector case before that model exists means building it
twice.

## Target + blast radius

`src/precis_se/toolaccess.py` (the swept-volume construction and the
try-tools-in-preference-order loop), `precis/data/driver_envelopes.json`,
and whatever the finding's renderable-volume hook turns out to be under
`se-mechanical-drc.md` ruling 6 — a sector is exactly the case where
showing the user the volume matters most.

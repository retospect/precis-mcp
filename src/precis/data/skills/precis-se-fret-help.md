---
id: precis-se-fret-help
title: precis — FRET links as a comm channel between se blocks (optical domain)
summary: model Förster resonance energy transfer between blocks — set_chromophore (the dye card on a template, dipole in the block frame), set_optical_link (the required min_efficiency on an existing connect), set_optics (medium index, pump wavelength) — then read view='fret'; the two traps are κ²=0 orientation and donors broadcasting to every acceptor in range
answers:
  - how do I model FRET between two blocks — energy transfer as a communication channel?
  - why is my FRET link dead even though the blocks are close enough?
  - how do I declare a required transfer efficiency and check the geometry against it?
applies-to: get/edit (kind='se'); read precis-se-help first for the op grammar
status: active
tags: verbs, design
kinds: se
---

# precis-se-fret-help — energy transfer as a comm channel

Read `precis-se-help` first for the op grammar, units and block
addressing. The ops below go in `edit(kind='se', id=…, ops=[…])`.

## Optical (FRET) ops — energy transfer as a comm channel

Use these when blocks talk to each other by **Förster resonance energy
transfer** — the channel is the geometry (no waveguide): efficiency falls
as `r⁻⁶` times an orientation factor `κ²` from the two transition
dipoles. Check it with `view='fret'`; the physics, its range of validity
and the solver are owned by the module docstring of
`precis/src/precis_se/fret.py` (`get(kind='python', id=…)`).

- `set_chromophore` — `block` (req) + the whole card: `label` (the dye,
  e.g. `"Cy3"`) · `dipole` `[x,y,z]` **in the block frame** (the block's
  own pose rotates it into world space, so an instance of a template
  inherits the chemistry and gets its own orientation) · `quantum_yield`
  (0–1) · `lifetime_s` (donor excited-state lifetime, seconds) ·
  `emission` `[[nm, value], …]` (arbitrary units) · `absorption`
  `[[nm, M⁻¹cm⁻¹], …]`. All fields required — a partial card would still
  produce a number, from physics that isn't there. `clear: true` removes
  it. Lives on the template, not on instances.
- `set_optical_link` — `a`, `b` (each `'block.port'`, addressing an
  existing connect like `set_joint` does) + `min_efficiency` (req,
  strictly 0–1) · `channel` · `reason`. The L2 declaration: what this
  link **needs**, stored, never derived. Both endpoints must already
  carry a chromophore — a transfer requirement between blocks with no
  optics is a typo, not an unmet requirement. `min_efficiency: null`
  clears. Compatible with `joint`/`kind` on the same connect: an optical
  link is different physics on the same pair, not a competing claim.
- `set_optics` — the design's optical context: `medium_index` (req; a real
  input to every Förster radius, not bookkeeping) · `excitation_nm` (the
  pump, enables the spectral-crosstalk figure) · `clear`. Undeclared is
  legal — the view then assumes ~1.4 and says so.

**Two traps worth knowing before you place anything.** (1) `κ² = 0` for
dipoles that are mutually perpendicular and both perpendicular to the
line between them: a geometrically perfect link that transfers nothing,
at any distance. The fix is rotating a block, not moving it. (2) A donor
is a **broadcast, not a wire** — every acceptor in range competes for the
same excitation, so `view='fret'` solves them together and a pair
efficiency read in isolation overstates the link.


## See also

- [[precis-se-help]] — the call surface: op grammar, units, views
- [[precis-se-states-help]] — a photoswitch's states and transitions, the usual partner of an optical link

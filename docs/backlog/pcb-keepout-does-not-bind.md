---
status: draft
title: pcb — an authored or imported keepout region constrains nothing, silently
prio: high
pillar: 3d-design
---

# pcb: `ftype='keepout'` is accepted and does nothing

Evidence: peer session easyeda-pcb-round-trip, 2026-09-30. `ftype='keepout'`
is an accepted `pcb_features` value — `src/precis/migrations/0047_pcb_kind.sql`
lists it in the `ftype` column comment
(`mounting_hole|fiducial|testpoint|keepout|outline`), and
`pcb-argue-with-design.md`'s anchor grammar documents `feature:outline` as
resolving to a `pcb_features` row with `ftype` drawn from that same set. But
the literal `'keepout'` appears **nowhere else** in `src/precis` — a repo
grep finds it only in that one migration comment. `drc.py`'s
`check_via_pad_keepout` / `check_via_via_keepout` are unrelated geometric
checks (via-to-pad and via-to-via clearance rules, not user-authored
keepout regions — their names collide with the concept but not the
mechanism). An authored or imported keepout region today constrains
nothing: placement can put a part on it, routing can run copper through it,
and no DRC finding fires.

Separately, `pcb_nets` has a `domain` column with `fluidic`/`thermal`
schema-reserved, and `src/precis/handlers/pcb.py::_reject_non_electrical`
refuses any non-`electrical` domain at write time ("v1 routes electrical
nets only" — `handlers/pcb.py:401`). That refusal is the intended door for
fluidic/thermal co-design (`cross-scale-single-assembly.md`), currently
locked.

## Motivation / why

`pcb-missing-constraint-classes.md`'s survey names crystal/oscillator
keepout as a §A correctness item but assumed the mechanism existed to hang
it on. It doesn't. A fluidic channel or a motor mount authored as a
mechanical keepout (the exact use case `cross-scale-single-assembly.md`
wants) is currently invisible to both the placer and the router — the
constraint is expressible in the schema and silently ignored everywhere
else.

## In scope

- **Keepout binds in placement + routing DRC**: a `pcb_features` row with
  `ftype='keepout'` becomes an obstacle the placer avoids and the router's
  DRC forbids copper crossing — same obstacle-primitive machinery the
  via/pad keepout checks already use (layer-masked polygon), just fed from
  user-authored features instead of only via geometry.
- **Warn on import when a keepout is present** — an EasyEDA/KiCad import
  that carries a keepout region today drops it silently; surface that as
  an explicit warning so a round-tripped board doesn't lose the constraint.
- **Name the fluidic/thermal door** — `_reject_non_electrical`'s refusal is
  the documented entry point for non-electrical nets; this item names it as
  such (pointer, not implementation) so `cross-scale-single-assembly.md`
  has a concrete seam to unlock rather than rediscovering it.

## Explicitly NOT in scope

- Unlocking `_reject_non_electrical` itself, or building fluidic/thermal
  net support — that's `cross-scale-single-assembly.md`'s scope; this item
  only names the door.
- New keepout *shapes* or authoring ergonomics beyond what `pcb_features`
  already carries — the defect is that the existing value does nothing,
  not that the value is expressively limited.

## Acceptance criteria

- A board with an `ftype='keepout'` feature rejects a placement that
  overlaps it (placer-side) and reports a DRC finding on copper routed
  through it (router-side).
- An imported board (EasyEDA/KiCad) whose source carries a keepout region
  either preserves it as an `ftype='keepout'` feature or emits an explicit
  import warning — never silent drop.
- A regression test pins `check_via_pad_keepout`/`check_via_via_keepout`
  as unaffected (they stay the via-geometry checks they are today; this
  item adds a sibling check, not a rename).

## Target + blast radius

`src/precis/pcb/drc.py` (new keepout-vs-copper/placement check, alongside
the existing via keepout checks), the placer (obstacle set —
`pcb-placer-obstacle-set-is-mounting-holes-only.md` is the sibling gap for
the placer side specifically), the EasyEDA/KiCad importers (warn-on-drop).

## Open questions / decisions log

- **Evidence correction (2026-10-02):** the "184 keepout records" on
  Reto's imported board are 16 `RULE` + 168 `RULE_SELECTOR`, most likely
  Pro's design-rule table and per-net assignments, not keepout areas
  (unverified; the bodies went with the `.epro2`). The defect here stands
  on the schema alone; the real-board motivation does not, until a rule
  body shows an area.

- Whether keepout enforcement is a hard placement/route refusal or a DRC
  finding a human can accept — leans DRC finding, matching the rest of the
  pcb DRC posture (report, don't silently refuse).

Closest existing items: `pcb-missing-constraint-classes.md` (the survey
that assumed this mechanism existed), `pcb-se-binding.md` (the mm-enclave
crossing this item's fluidic/thermal door feeds), `pcb-argue-with-design.md`
(the anchor grammar that already names `feature:outline`'s `ftype` set).
Same defect class as `pcb-placer-obstacle-set-is-mounting-holes-only.md`
(ewod-pcb Do-next 3): authored geometry the engine silently ignores — there
via an incomplete obstacle set, here via an unimplemented `ftype`. Whoever
implements either should read both.
Thread: `docs/backlog/threads/pcb-easyeda-round-trip.md` (sequenced behind
`docs/backlog/threads/ewod-pcb.md` on the generator/DRC files it touches).

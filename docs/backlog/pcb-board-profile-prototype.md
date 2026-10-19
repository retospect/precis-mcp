---
status: ready
title: pcb — a board `profile` (`prod` | `prototype`) that tunes the layout for hand assembly and rework
pillar: 3d-design
prio: normal
---

# pcb: board profile `prototype`

## Motivation / why

A board that a person will solder, probe and rework wants different
copper from one a machine assembles once. Today there is one behaviour:
pours tie pads solid (`src/precis/pcb/planes.py` docstring: thermal
reliefs "are not modelled"), vias may sit under a part, the DRC holds the
fab's `house_default` tier as a warning, and trace/space/ring are sized
for JLC's process, not for a soldering iron and a probe tip.

Reto, 2026-10-17 on `/pcb/usbc-attiny-dogfood-b`: "penalize (only a
little) vias that are under components … only when devboard is on (ie
thermals, and this). Devboard goal is to make debugging easier for humans.
Prodboard (the default) has no such restriction." Reto, same day: the name
is `prototype`, not `dev`. Reto, 2026-10-18: "promote. via density,
thermals, etc." Gripe: gr495399. This file replaces the idea item
`pcb-prototype-thermal-reliefs` (deleted in the same commit); thermal
reliefs are now one rule of the profile, not the whole feature.

One board key, `profile`, selects a bundle of rules. `prod` is the
default and is today's behaviour exactly. The profile is a *bundle of
overrides on existing seams*, not a second engine: every rule below
rewrites a number or a switch that an existing function already reads.

## In scope

### The key

`profile: 'prod' | 'prototype'`, stored on `pcb_boards.meta['profile']`
(absent ⇒ `prod`), the same home and shape as the authored `phase`
(`PcbHandler._op_phase` in `src/precis/handlers/pcb.py`, persisted by the
`phase` setter in `src/precis/store/_pcb_ops.py`). Set with
`put(kind='pcb', args={'op':'profile','profile':'prototype'})`, and at
create with `args={'profile': …}`. Anything else is refused with
`options=['prod','prototype']`. The board summary prints
`profile: prototype` only when it is not `prod` (so prod summaries do not
change). The route summary reports the profile's measured effects (below).

Profile values are data, in `src/precis/data/pcb_profiles.json` beside
`pcb_capabilities.json`, one row per profile, each field carrying a
one-line reason. Rules read the profile through one accessor,
`precis.pcb.profiles.profile_rules(name) -> ProfileRules` (frozen
dataclass; `prod` is the all-`None` / all-off instance). No consumer
branches on the string `'prototype'`.

### What `prototype` changes — and the default values

All defaults are for a 4-layer board assembled by hand (iron + hot air),
probed with a multimeter or scope clip, and reworked in place. "Prod" is
today's value. Every figure is a floor/ceiling applied on top of the fab
row: it can raise a number, never take one below `jlc_min`.

| Rule | prod (today) | prototype default | Why |
|---|---|---|---|
| Min trace width (signal, `trace_width_mm`) | `house_default` 0.15 mm | 0.20 mm | survives a lifted pad and iron heat; legible on a scope |
| Min trace/space (`trace_spacing_mm`, and the `clearance_mm` a net class resolves to) | `house_default` 0.15 mm | 0.20 mm | solder bridges and probe tips stay off the neighbour |
| Via drill (`drill_mm`) | `house_default` 0.25 mm | 0.30 mm | a wire-wrap or probe wire fits; drill survives rework |
| Via annular ring (`annular_ring_mm`) | `house_default` 0.25 mm | 0.25 mm (unchanged) | via diameter is derived `drill + 2 × ring` = 0.80 mm; the ring is not the lever |
| Via diameter | derived, 0.75 mm | derived, 0.80 mm | follows drill; `capabilities._derive_via_diameter_mm` stays the one place that computes it |
| Via under a part's courtyard | extra route cost `maze.VIA_UNDER_BODY_COST_MM` = 3.0 | `via_body_cost_mm` = 6.0 | a soft cost, not a keep-out: Reto "only a little". See the note below |
| Via density | no limit | at most 1 via per 4 mm² of board in any 10 × 10 mm window, soft: warn, never block | vias are what a person has to dig past; the limit is on windows, not the board average |
| Thermal relief on plane/pour pads | solid | spokes + antipad gap | hand soldering a pad tied to a plane pulls heat away |
| Silk | stroke `silk_width_mm` house 0.25 mm, refdes height `silk.DEFAULT_REFDES_HEIGHT_MM` = 1.0 mm | stroke unchanged; refdes height 1.2 mm; pin-1/polarity mark on every polarized part (`session.polarized_refdes` already finds them; the profile makes a missing mark a warn) | a person reads the board; the label has to survive a magnifier-free glance |
| DRC strictness | `jlc_min` shortfall = error, `house_default` shortfall = warn | `house_default` shortfall = error for trace width, spacing, drill, ring; other rules unchanged | the profile's numbers *are* the new floor; a warn on a number the profile set is noise |
| Prototype-only checks | none | `starved_thermal`, `via_under_part` (info, with count), `fine_pitch_pad` (warn below 0.5 mm pitch: hard to hand solder) | see below |

**Via cost note.** The under-part via penalty is **already on for every
board**: `VIA_UNDER_BODY_COST_MM` (`src/precis/pcb/maze.py`) is the
default of `RealizeConfig.via_body_cost_mm` (`src/precis/pcb/realize.py`),
and `_courtyard_body_mask` builds the mask. The gripe's framing (penalty
only on `dev`) therefore does not hold for the code as it stands. The
rule keeps prod untouched (3.0, so prod boards are byte-identical) and
makes the profile double it. If Reto wants prod's penalty removed or the
prototype penalty to differ, that is a one-number change in the JSON row.

**Via density.** The window rule is a DRC finding (`via_density`, warn),
not a router constraint; the router already pays `VIA_COST_MM` per via.
Windows slide on a 2.5 mm step over the outline's bounding box and count
vias whose centre falls inside. Test-point vias are exempt once test
points exist (see "Later rules").

**Thermal reliefs.** On a pour-connected *pad* (SMD or through-hole;
vias stay solid so plane stitching and return paths are untouched):

- `thermal_spokes` = 4, `thermal_spoke_width_mm` = 0.30 (≥ the prototype
  trace-width floor), `thermal_gap_mm` = 0.25, spokes at 45° to the
  axes so they miss the usual orthogonal escape trace.
- The pad's net must still be one connected island after the relief
  (`precis.pcb.connectivity.net_islands` is the check); a pad whose
  relief would leave fewer than 2 spokes after other-net copper is cut
  stays solid and is reported (`starved_thermal`, warn), not silently
  isolated.
- A class may force `thermal: 'solid'` (power pads that carry current:
  the class value wins over the profile for current-carrying nets).
  The per-class key and the spoke generator are
  [pcb-stackup-regions](pcb-stackup-regions.md) slice 10; this item sets
  the profile default for that key and adds nothing to the class model.

### Where it plugs in (checked against the tree, 2026-10-18)

One overlay, applied once, read everywhere:

1. **Capability overlay** — `precis.pcb.capabilities`
   (`CapabilityRow`, `design_value`, `headroom`, `load_capabilities`)
   gains `apply_profile(row, rules) -> CapabilityRow`, returning a row
   whose `house_default` for the profiled fields is
   `max(row.house_default, profile value)` and whose `jlc_min` is
   untouched. Because `design_value` and every `_two_tier` consumer read
   the row, trace width, spacing, drill, ring and the derived via
   diameter all follow without per-site edits.
   `session.stackup_capability(stackup)` (`src/precis/pcb/session.py`)
   is the single lookup the handler (`PcbHandler._stackup_capability`),
   the `pcb_argue` worker and the DRC render share; it gains a
   `profile=` argument and applies the overlay. This is the plumbing
   cost: its callers pass the board's profile (read from `board.meta`).
2. **Net rules** — `precis.pcb.rules.resolve_net_rules` takes the
   already-overlaid `fab_caps`, so `NetRules.track_width_mm` /
   `clearance_mm` / `via_dia_mm` follow; `session.clearance_net_rules`
   is the shared caller. No change to the resolver's signature.
3. **DRC strictness** — `precis.pcb.drc._two_tier` returns
   `("error", …)` below `house_default` when the profile says
   `strict_house_tier` for that field. The check functions already pass
   `field` (`check_trace_width`, `check_annular_ring`, `check_clearance`;
   no drill-size check exists yet, so the drill floor is enforced by the
   derived via diameter and the ring check); the flag is read from the same capability row via a
   new `CapabilityRow.strict_fields: frozenset[str]` set by the overlay.
   New rules `starved_thermal`, `via_density`, `via_under_part` and
   `fine_pitch_pad` register beside the existing `check_*` functions
   and are emitted only when the profile enables them (prod boards never
   see the rule name, so prod DRC output is unchanged).
4. **Router cost** — `RealizeConfig.via_body_cost_mm`
   (`src/precis/pcb/realize.py`) is set from the profile at the call
   that builds the config; `maze.OccupancyGrid.route(via_body_cost_mm=)`
   already takes it. The route summary adds `vias_under_part: N`, counted
   from the same `body_mask` (`_courtyard_body_mask`) over the realized
   vias, printed for every profile (a prod board gets the number too, as
   a new line — the one deliberate summary change; copper stays
   identical).
5. **Thermal geometry** — `precis.pcb.planes.plane_pours` subtracts
   `pad ⊕ gap − pad` for each relief pad and adds back the spokes; the
   result is ordinary `ctype='pour'` polygon items with holes, so
   `precis.pcb.gerber` and `precis.pcb.epro_write` already emit them
   *if* both writers carry interior rings — acceptance test below
   proves it rather than assuming it.
6. **Silk** — `precis.pcb.silk` (`DEFAULT_REFDES_HEIGHT_MM`,
   `build_silk`'s `height_mm`) takes the height from the profile;
   `silk_clearance_mm` is untouched (its chain is fab-driven).
7. **Profile data and accessor** — new `precis.pcb.profiles` +
   `src/precis/data/pcb_profiles.json`; the schema is `meta` JSON, so no
   migration.

### Slices

Slices 1 (key, `pcb_profiles.json`, `profile_rules`, summary line) and 2
(capability overlay `capabilities.apply_profile`, `session.board_capability` /
`stackup_capability(profile=)`, `strict_fields` in the DRC `_two_tier`, and
the load-time floor check in `profiles._check_floors`) shipped. The overlay
is read by the handler, the DRC render, `pcb_route` and `pcb_argue`; a
`prototype` 0.17 mm trace is an error, `prod` stays a warn. Drill strictness
has no consumer until a drill-size check exists.

1. **Router cost, `vias_under_part` count, `via_density`,
   `via_under_part`, `fine_pitch_pad`, silk height.** Items 4 and 6.
2. **Thermal reliefs** (item 5) and `starved_thermal`. Lands after
   [pcb-stackup-regions](pcb-stackup-regions.md) slice 10 (it owns the
   spoke generator and the per-class key); this slice then only sets the
   profile default and proves the exports.

## Explicitly NOT in scope

- Later prototype rules, listed so the table has room and none is
  built here: test points on every net (and exempting them from
  `via_density`); probe-able nets (a named set that gets a pad or a
  loop); larger silk (pin numbers, net names beside test points);
  jumper/solder-bridge pads on rails; no `0201` or smaller passives; a
  hand-assembly part filter (no QFN/BGA) in `part` selection.
- A keep-out for vias under parts. Reto: a soft cost, "only a little".
- Changing `prod` in any way. Prod boards are byte-identical before and
  after (nano, EWOD, taz15 baselines); the one allowed difference is the
  new `vias_under_part` summary line.
- A third profile, or per-board overrides of individual profile values.
  If a board needs 0.25 mm space it sets a net-class `clearance_mm`,
  which already beats the profile's floor upward (the overlay raises
  `house_default`; an authored class value above it still wins).
- Fab-capability rows for non-JLC processes.
- Pour on every layer (gr495395) and vias off silk (gr495393): separate
  items; this profile composes with both.

## Acceptance criteria

1. **Prod unchanged.** For the nano, EWOD and taz15 baselines, route +
   export output (Gerber, EPRO, DRC finding set) is byte-identical to
   before the change, apart from the new `vias_under_part` summary line.
   A test pins the finding set and the export hash of one small prod
   board (and `PRECIS_UPDATE_EXPORT_GOLDEN` handling stays as is).
2. **Key.** `op='profile'` round-trips through `put`/`get`; an unknown
   value is refused with `options=`; absent ⇒ `prod`; the summary shows
   `profile: prototype` only for prototype.
3. **Overlay.** For a 4-layer board, `design_value(apply_profile(row,
   prototype), 'trace_spacing_mm')` is 0.20 and `jlc_min` is unchanged; the
   derived via diameter is 0.80. A profile row asking for a value below
   `jlc_min` is rejected at load.
4. **Strictness.** A 0.17 mm trace is a `warn` on `prod` and an `error`
   on `prototype` (same board, same run); a 0.08 mm trace is an `error`
   on both.
5. **Via penalty.** With `prototype`, the realized board on
   `usbc-attiny-dogfood-b` has fewer `vias_under_part` than the same board
   on `prod`, and routed-net count does not drop (soft cost: report both
   numbers in the commit). If routed nets drop, halve the penalty and
   re-measure before landing.
6. **Density.** `via_density` fires on a synthetic board with 6 vias in a
   2 × 2 mm window and not on the same vias spread across 10 × 10 mm.
7. **Thermals.** On `usbc-attiny-dogfood-b` with `prototype`, every
   through-hole and SMD GND pad on a poured layer carries 4 spokes of
   0.30 mm or is reported by `starved_thermal`; `net_islands` still
   reports each poured net as one island; the exported Gerber and EPRO
   contain the antipad holes (assert on the polygon rings, not a
   screenshot). A class with `thermal: 'solid'` stays solid under
   `prototype`.
8. **DRC output on prod is silent** about the four new rules.

## Target + blast radius

`src/precis/pcb/` — `capabilities.py`, new `profiles.py`, `session.py`
(`stackup_capability`, `clearance_net_rules`), `rules.py` (read-only
consumer), `drc.py` (`_two_tier`, new checks, rule registry),
`realize.py` (`RealizeConfig.via_body_cost_mm`), `planes.py`
(`plane_pours`), `silk.py`, `gerber.py`/`epro_write.py` (verify only);
`src/precis/handlers/pcb.py` (`_op_profile`, summary);
`src/precis/store/_pcb_ops.py` (board-meta setter, next to `phase`);
`src/precis/data/pcb_profiles.json`; skills `precis-pcb-help` and `precis-pcb-check-help` gain one
paragraph. Every call of `session.stackup_capability` changes signature.
No migration. Prod boards are the blast radius to protect: criterion 1.

## Open questions / decisions log

- Decided (Reto 2026-10-17/18): the name is `prototype`; promote; scope
  is "via density, thermals, etc."
- Defaults in the table are proposals chosen to be a visible but modest
  step above `house_default`; Reto may tune them in the JSON without
  touching code. No blocker.
- Prod's existing under-part via cost (3.0) stays; flagged above because
  the gripe assumed none existed.

---
id: precis-se-enclosure-help
family: se
title: precis — generate an enclosure box and mating lid
summary: create or resize a rounded enclosure with a mating lip, screw bosses and optional ribs; dimensions, draft, clearances and validation for enclosure_box
answers:
  - how do I generate a printed enclosure with a matching lid?
  - how do I size enclosure walls, draft, screw bosses and the lid lip?
  - why did enclosure_box reject my dimensions or screw layout?
applies-to: put/edit(kind='se') with the enclosure_box op
status: active
tags: workflow, design
kinds: se
---

# precis-se-enclosure-help — a box and its mating lid

Generate a rounded enclosure as two CAD-bound `se` blocks, `box` and `lid`, using `enclosure_box`; use [[precis-se-flatpack-help]] for panels cut from sheet material.

## Create or resize an enclosure

Supply `{"op":"enclosure_box","params":{...}}` through `put` or `edit`; a fresh title creates a design, while `edit` preserves the rest of an existing design. Generated CAD designs are `<design>-box` and `<design>-lid`, already in assembled coordinates with zero block poses. Repeating the op replaces its two parts under the same names; supply the full desired parameter set because omitted options take their defaults. Unrelated blocks or CAD designs occupying those names are refused; create a separately named enclosure.

```python
put(kind='se', title='Sensor enclosure', ops=[
    {'op': 'enclosure_box', 'params': {'x': 80, 'y': 50, 'z': 25}}
])
edit(kind='se', id='<design>', ops=[
    {'op': 'enclosure_box', 'params': {'x': 80, 'y': 50, 'z': 30}}
])
```

## Set the cavity, walls and draft

All lengths are millimetres; `x`, `y` and `z` are the inner cavity width, depth and height, with `x`/`y` measured at the cavity floor. Defaults are `wall=2`, `floor=2`, `lid_thickness=3`, `corner_radius=4` and `draft_angle=0`; the radius is the inner corner radius at the cavity floor. `draft_angle` is degrees, from 0 inclusive to 15 exclusive: positive draft opens toward the lid while preserving wall thickness normal to the sides. Zero draft suits straight printed walls; positive draft provides a mould-release taper, but does not establish that a complete part can be moulded. The lid plate matches the rim's outer dimensions.

## Seat the lid with a locating lip

`lip_height=3` inserts a ring beneath the lid; `lip_clearance=0.3` is the positive gap per side, and the ring follows the wall thickness and draft. The floor, lid and wall thicknesses must be positive; `corner_radius` must be below half the smaller inner dimension, and `lip_height + lip_clearance` must be below `z`. The lip opening must remain wide enough for the selected walls, corners and bosses; reduce wall/lip dimensions or increase the cavity when those features consume the opening.

## Place screw bosses and support ribs

`boss_count=4` places bosses inside the lip opening; counts 0–4 are supported, with two bosses at opposite corners. `boss_placement=[[x,y],...]` overrides automatic placement using millimetres from the centred XY origin and must contain exactly `boss_count` pairs. `boss_outer_diameter=8` must leave at least one `wall` thickness around the pilot hole; every boss must clear the cavity wall, lip and other bosses. `ribs=True` joins each boss to its nearest wall below the inserted lip; `ribs=False` removes those supports. Rib thickness is `rib_ratio * wall`, with `rib_ratio=0.55` and an allowed range of 0.5–0.6.

## Choose screw holes and a head recess

`screw_size='M3'` selects a blind thread-forming pilot in each boss and a matching through-clearance hole in the lid; supported sizes are M2, M2.5, M3 and M4. Pilot sizes use rigid-thermoplastic starting rules, and clearance holes use the house clearance rule (nominal diameter plus 0.2 mm). `counterbore=False` is the default; `True` adds recesses only to the lid, sized to head diameter plus 0.4 mm and head height. `lid_thickness` must be strictly greater than head height plus 1 mm (M3 requires more than 4 mm). Head dimensions are house socket-cap starting envelopes, so confirm the actual screw head and material before fabrication; sizing alone guarantees neither print fit nor thread strength.

## Recover from a rejected layout and inspect fit

Invalid dimensions and placements raise `BadInput` before geometry is built; follow the named parameter correction and resubmit the full parameter set. Screw circles must stay inside the walls, bosses must fit inside the lip with clearance, and boss centres must be separated by at least `boss_outer_diameter + lip_clearance`. Assembled solids intentionally touch at the rim and boss tops; the lid's conservative envelope includes empty space below it, so envelope overlap alone does not establish solid interference. [[precis-se-fit-help]] describes checking the bound CAD geometry and interpreting contact findings.

## See also

[[precis-se-help]] covers design edits and views; [[precis-se-print-help]] covers printing and export; [[precis-se-fasten-help]] covers fastening and thread strategies.

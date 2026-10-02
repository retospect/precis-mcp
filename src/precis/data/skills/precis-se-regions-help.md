---
id: precis-se-regions-help
title: precis — region properties on an se design (measurands, region selectors, pockets)
summary: declare non-geometric properties on part of a block — "this side hydrophobic, that patch negative" — as ordinary se measures that name a taxonomy measurand (surface charge density, contact angle, net partial charge, dipole moment, H-bond donor/acceptor count, electric field magnitude, absorption maximum wavelength, hydrophobicity index) and a region selector (patch:/ring: on a face, sites:/atoms: on bound atoms); group regions into a named pocket with add_pocket and read them back with view='pockets'. Nothing computes these values yet — view='drc' flags each as measurand_unchecked
answers:
  - how do I say one face or patch of a block is hydrophobic, charged or polar?
  - how do I declare a binding pocket with regions of different charge?
  - what measurands can an se measure use besides metres, counts, ratios and degrees?
  - how do I address part of a face, a rim, a seam span or specific atoms in a selector?
  - why does view='drc' report measurand_unchecked?
  - why is my measurand refused or ambiguous?
applies-to: get/edit/put (kind='se'); search/get (kind='taxon') to find a measurand; read precis-se-help first for the op grammar
status: active
tags: design
kinds: se, taxon
---

# precis-se-regions-help — properties on part of a block

A region property is an **ordinary se measure** with two extras: a
`measurand` (what is measured, from the term taxonomy) and a `datum` that
selects a **region** (where on the block). A **pocket** names a set of
regions on one block and gives them a shape. A bare property is not a
separate object: everything below is `add_measure`.

## Declare a property with a measurand

```python
edit(kind='se', id='rotor', ops=[{
    'op': 'add_measure', 'block': 'cavity', 'name': 'q_floor',
    'measurand': 'surface charge density',      # slug, name, path or tn<id>
    'datum': 'patch:cavity.bottom@0,0+8e-10x4e-10',
    'min': -1.0, 'max': -0.5, 'strength': 'hard'}])
```

- `measurand` takes a slug (`surface-charge-density`), the name, a path
  (`measurand/contact-angle`), `tn<id>` or the numeric id. It must sit
  under the `measurand` root; an unknown or ambiguous one is refused with
  the candidates.
- The **unit comes from the measurand** and is stored with the measure:
  `C/m^2`, `e` (net partial charge), `D` (dipole moment), `V/m`, `deg`,
  `count`. Every length is in metres, whatever unit the taxonomy node
  quotes. Passing a `unit=` that disagrees is refused; omit it.
- `unit='m'|'count'|'ratio'|'deg'` still works unchanged. Those four are
  the measurands `length`, `count`, `ratio` and `angle`.
- A **categorical** measurand (`hydrophobicity index`: hydrophilic |
  amphiphilic | hydrophobic) takes no `value`/`min`/`max`/`relation`
  source. The measure states that the property matters there; use
  `contact angle` for a numeric band.
- Relations need unit agreement: relate a surface charge density only to
  another surface charge density. A mismatch is a `unit_mismatch` error
  in `view='drc'`.
- `set_measure measurand=` changes what a measure measures. `set_measure
  unit=` on a measurand measure is refused.

Find a measurand:

```python
search(kind='taxon', under='measurand', q='charge')
```

## Select a region

| selector | addresses | resolves to |
|---|---|---|
| `patch:<block>.<face>@<u>,<v>+<w>x<h>` | a w×h rectangle on a face, centred (u, v) from the face centre, metres | patch centre + face normal |
| `ring:<block>.<face>` | the boundary loop of a face (a rim, an edge loop) | loop centre + face normal as axis |
| `sites:<block>/<seam>/s<i>..s<j>` | seam sites i to j of a hexfold block | not resolved yet (see below) |
| `atoms:<block>[0,3,5-9]` | atom ordinals in the block's bound structure | not resolved yet |

- `u` runs along the face's first in-plane axis: the block's local +x
  projected onto the face, or local +y when the face normal is within 5°
  of ±x (a face that near perpendicular to x has no stable +x
  projection). `v` = normal × u. The patch moves with the block on
  `set_pose`.
- Face names are the envelope's own: `top`, `bottom`, `side0`… A patch
  centre outside the face's extent resolves to an error note.
- Write numbers in metres. A patch larger than its face (`8x4` for
  `8e-10x4e-10`) is noted, flagged `patch_exceeds_face`.
- The face check uses its bounding rectangle. A face that is not a
  rectangle (area off by more than 1 %, or a disc or other curved face)
  gets "bounds approximate" in the note.
- Block names may not contain `/`, `@`, `[` or `]` (selector delimiters;
  `add_block`, `instance_block` and `array_block` refuse them). Dots are
  fine.
- The shape is checked when you write, existence when you read. A
  malformed selector is refused and the error lists the whole grammar;
  a well-formed one naming a face that is not there yet is accepted.
- `sites:`/`atoms:` are accepted on a block bound to a structure design.
  Their coordinates are not loaded yet, so a read returns a note saying
  so instead of a value. Both are pinned to a structure version (below).
- `frame`, `port:<name>`, `face:<block>.<tag>`, `axis:<block>` and the
  `face:` predicates work too (`precis-se-help`).

## Group regions into a pocket

```python
edit(kind='se', id='rotor', ops=[{
    'op': 'add_pocket', 'block': 'cavity', 'name': 'ratchet_site',
    'shape': 'sphere:r6e-10',                    # cad DSL, block frame; or 'hull'
    'regions': [
        {'selector': 'patch:cavity.side0@0,0+4e-10x4e-10',
         'measures': [{'name': 'attract', 'measurand': 'net partial charge',
                       'max': -0.3, 'strength': 'hard'}]},
        {'selector': 'patch:cavity.side2@0,0+4e-10x4e-10',
         'measures': [{'name': 'reject', 'measurand': 'net partial charge',
                       'min': 0.3, 'strength': 'hard'}]},
        {'selector': 'ring:cavity.top',
         'measures': [{'name': 'rim', 'measurand': 'hydrophobicity index'}]}]}])
```

- Each inline measure is written as `add_measure` with the pocket's
  `block` and the region's `selector` as `datum`. Do not pass `block` or
  `datum` inside it.
- A region's measures are **every** measure on that block whose `datum`
  names its selector. An `add_measure` written later with the same
  selector joins the region.
- `set_pocket` — `block`, `name` + `shape` and/or `regions` (replaces
  the region list; inline measures are added). `remove_pocket` —
  `block`, `name`. It drops the pocket only; its measures stay.
- Pockets, like measures, live on a template block, never on an instance.

## Pins: atom and site numbers belong to one structure version

An atom ordinal means something only against one version of the bound
structure design (its version rises on every save). Seam site numbers
are pinned the same way, because a regeneration may renumber them.

- Writing an `atoms:`/`sites:` measure (`add_measure`, `set_measure`,
  inline pocket measures) stamps `datum_pin` = `<structure-slug>@v<n>`
  from the block's bound structure. A block bound to nothing gets no pin.
- If the block is later bound to another structure or a later version,
  the read note says so, `view='drc'` warns `region_pin_stale`, and
  `view='pockets'` marks the line STALE. Re-declare the region
  (`set_measure datum=`) to pin it to the version now bound.
- `datum_pin` is exported in `view='ops'` and kept on replay; do not
  pass it by hand.
- `atoms:`/`sites:` are refused as a `relation` `feature` until atom-level
  computers land: they carry no pin there. Use them as the `datum`.

## Region findings in view='drc'

All warn, one per measure (subject `<block>.<measure>`):

- `datum_unresolved` — the datum names a missing block, face or envelope,
  or a patch centre off its face. The detail has the selector and the
  resolver's error. `sites:`/`atoms:` on an existing block are exempt.
- `patch_exceeds_face` — a patch rectangle reaches past its face.
- `region_pin_stale` — see Pins.
- `measurand_unchecked` — see the last section.

## Read regions back

```python
get(kind='se', id='rotor', view='pockets')    # one section per pocket
get(kind='se', id='rotor', view='measures')   # measurand shown as [slug]
get(kind='se', id='rotor', view='drc')        # measurand_unchecked per property
get(kind='se', id='rotor', view='ops')        # round-trips measurand + pockets
```

`view='pockets'` lists each region and one line per measure: measurand,
unit, band, strength and the realised value. A design with no pockets
returns an empty list.

## Why view='drc' says measurand_unchecked

No property computer is live yet. Every measure whose measurand is not
`length`, `count`, `ratio` or `angle` is stored intent: its band is
recorded, nothing computes a value to check it against, and DRC warns
about each one instead of passing it silently. The `realised` column in
`view='pockets'` stays `—` until a computer covers that measurand.

A measure keeps the measurand's taxon id; the slug shown follows a
rename of the taxon node.

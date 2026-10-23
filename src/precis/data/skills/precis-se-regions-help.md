---
id: precis-se-regions-help
family: se
title: precis — region properties on an se design (measurands, region selectors, pockets)
summary: declare non-geometric properties on part of a block — "this side hydrophobic, that patch negative" — as ordinary se measures that name a taxonomy measurand (surface charge density, contact angle, net partial charge, dipole moment, H-bond donor/acceptor count, electric field magnitude, absorption maximum wavelength, hydrophobicity index) and a region selector (patch:/ring: on a face, sites:/atoms: on bound atoms); group regions into a named pocket with add_pocket and read them back with view='pockets'. Nothing computes these values yet — view='drc' flags each as measurand_unchecked
answers:
  - how do I say one face or patch of a block is hydrophobic, charged or polar?
  - how do I declare a binding pocket with regions of different charge?
  - what measurands can an se measure use besides metres, counts, ratios and degrees?
  - how do I address part of a face, a rim, a seam span or specific atoms in a selector?
  - why does view='drc' report measurand_unchecked?
  - why is my measurand refused or ambiguous?
  - how do I see where a bound molecule is charged, H-bond donating or accepting, polar or nonpolar?
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
| `sites:<block>/<seam>/s<i>..s<j>` | seam sites i to j of a hexfold block | the sites' atom ordinals on the bound structure (when the scene resolver is wired) |
| `atoms:<block>[0,3,5-9]` | atom ordinals in the block's bound structure | the atoms' ordinals + centroid |
| `mpatch:<mol>:<id>` | patch id of the molecule block's painted surface — the wall region opposite it | the wall atoms within clearance + 3 Å of the patch's points |
| `mol:<mol>@<clearance Å>` | the molecule's whole surface — the wall region around it | the wall atoms within clearance + 3 Å of any surface point |
| `mcone:<mol>:<atom>@<dx>,<dy>,<dz>/<half°>+<length Å>` | a cone with its apex on one molecule atom, axis in the molecule structure's own frame (it turns with the block's `rot`) | the wall atoms whose centres sit inside the cone |

`<mol>` is a block (`nh3`) or a block minus one atom of its structure
(`nh3!aH3`, the held fragment of pocket_from_molecule's `remove=`).

A pocket's `shape` also accepts `mol:<mol>@<clearance Å>` — the
molecule's surface offset outward by the clearance (provisional 0;
clearance < 0 is refused, 0 allowed).

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
- Block names may not contain `/`, `@`, `[`, `]` or `!` (selector delimiters;
  `add_block`, `instance_block` and `array_block` refuse them). Dots are
  fine.
- The shape is checked when you write, existence when you read. A
  malformed selector is refused and the error lists the whole grammar;
  a well-formed one naming a face that is not there yet is accepted.
- `sites:`/`atoms:` are accepted on any existing block, bound or not.
  On a block bound to a structure design the measure is pinned to that
  structure version (below). On an unbound block it is accepted with
  **no pin** and a read says "block binds no structure design" — bind
  first (`bind_structure`, or generate), then declare the region, or the
  indices are never version-checked. Their coordinates are not loaded
  yet, so a read returns a note instead of a value.
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

All warn except `pocket_keep_open`, one per measure (subject
`<block>.<measure>`):

- `pocket_keep_open` (**error**) — a `count` measure with `max: 0` on an
  `mcone:` region (a held-shape pocket's keep-open cone) has a wall atom
  inside the cone. The detail names the region and its pocket. Any other
  `count` band on an `mcone:` region that is missed (a hold cone with no
  wall atom) is `measure_unmet`.

- `datum_unresolved` — the datum names a missing block, face or envelope,
  or a patch centre off its face. The detail has the selector and the
  resolver's error. `sites:`/`atoms:`/`mpatch:` on an existing block are
  exempt.
- `patch_exceeds_face` — a patch rectangle reaches past its face.
- `region_pin_stale` — see Pins.
- `measure_unmet` — a measure whose measurand has a computer (net
  partial charge, H-bond donor/acceptor count, electrostatic potential)
  was realised outside its declared target/band — the detail names the
  realised and required values.
- `measure_uncomputable` — the computer ran but returned no value; the
  detail carries its reason (structure not loadable, no atoms in the
  region's cut, no GFN2 backend).
- `measurand_unchecked` — see the last section.

## pocket_from_molecule

`{'op': 'pocket_from_molecule', 'molecule': '<block>', 'wall': '<block>',
'z_window': [z0, z1], 'clearance_A'?: 0}` — a handler-level op (needs GFN2
and bound structures): it paints the molecule block's surface (the
`view='surface'` machinery), clusters it into patches, and writes one
pocket on the wall block named `<wall>-<molecule>@z<z0>-<z1>r<a>-<b>-<c>` (window in integer Å, so
[2.0, 4.5] nm is `@z20-45`; `r…` is the molecule's canonical body frame
→ wall rotation in integer degrees, x-y-z Euler as the block `rot`, `m`
for minus as in `r0-m90-0`, omitted when all zero; a linear molecule's
undefined spin reads 0. Same name = same window and rotation, so a
second pocket there refuses — move the window or rotate the molecule)
with shape
`mol:<molecule>@<clearance>` and one `mpatch:` region per patch whose
centroid z (in the wall frame) sits inside `z_window` (nm). Each region
carries the measures the patch implies: the mirrored ESP band
(electrostatic potential), an H-bond donor count ≥ 1 opposite each
acceptor patch, and hydrophobicity index `hydrophobic` opposite each
nonpolar patch. The pocket's `class_hash` (sha256 over the canonical
region list, derived and never authored) identifies the spec class —
same pose, same hash.

**Held shape** — add `'remove': '<atom label>'` (e.g. `'aH3'` of an NH3
block) and the pocket is for the fragment `<molecule>!<label>` frozen at
the parent geometry (an entatic, transition-state-like shape). Optional
`charge` (must equal the fragment's formal-charge sum), `multiplicity`
(default: the lowest the electrons allow — 2 for NH2) and
`spin_polarization` (default `false`; spin-polarised GFN2 when `true`).
The op computes, at GFN2, the strain E(frozen) − E(relaxed free) and
says it in its echo with the vacancy direction (from the removed atom's
neighbour toward it, world frame). The pocket is named for the fragment
(`wall-nh3!aH3@z…`), its shape is `mol:nh3!aH3@<clearance>`, and on top
of the fragment's patch regions it carries:

- a keep-open `mcone:` on the neighbour along the vacancy (30°, 3 Å +
  clearance) with `count` max 0 — a wall atom inside is DRC's
  `pocket_keep_open` error;
- a hold `mcone:` on each atom that moves ≥ 0.01 Å when the fragment
  relaxes free, along that motion (45°, 3.5 Å + clearance), `count` min 1
  — a wall atom must block it;
- an H-bond partner `mcone:` on each donor H along X→H (30°, 2.5 Å +
  clearance), `H-bond acceptor count` min 1;
- `binding energy` on `mol:<fragment>@<clearance>` with max −strain (eV):
  a placeholder until the H-addition requirement's form is settled; its
  reason says so. Nothing computes it yet (measurand_unchecked).

Cone sizes are provisional. GFN2 is a screen: on NH2 frozen at GFN2's
own NH3 geometry the strain is 0.006 eV.

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

## Paint a molecule's surface — view='surface'

```python
get(kind='se', id='nano', view='surface',
    args={'block': 'no', 'multiplicity': 2})          # per-atom table
get(kind='se', id='nano', view='surface',
    args={'block': 'no', 'format': 'json'})           # every point
```

One GFN2-xTB single point on the block's bound molecule, run on every
read and never stored, then a van der Waals surface painted with:

- **ESP** (V): a point-charge sum over the GFN2 Mulliken charges.
- **H-bond class**: `donor` on H bonded to N or O (direction X→H),
  `acceptor` on N or O with a lone pair (valence electrons − formal
  charge − neighbours ≥ 2; direction away from the neighbours), else
  `none`. The rule ignores pi delocalisation, so a pyrrole or amide N
  reads as an acceptor.
- **Polarity**: `polar` on N, O, F and H bonded to them, else
  `nonpolar`.

Each point also carries its position and outward normal in the se
world frame (the block pose applied, Å) and the atom whose sphere it
lies on. `format='json'` returns them all, the payload for a 3D
viewer.

- `charge` (e) defaults to the sum of the atoms' declared charges.
  `multiplicity` (2S+1) defaults to the lowest the electron count
  allows: NO (15 electrons) reads as a doublet. Pass it for a high-spin
  ground state (O2 is a triplet). A multiplicity that does not fit the
  electron count is refused.
- Surface settings are **provisional**: Bondi radii × 1.0, 3 points/Å²,
  no probe. `scale` (0.5–3) and `density` (points/Å², up to 20)
  override them for one read.
- No per-atom spin population: the GFN2 run is not spin-polarised, and
  the read says so.
- Needs tblite in the serving process. Without it the read is
  `Unsupported`; it is not dispatched to a compute node. The limit is
  200 atoms, and the structure must be one isolated molecule.
- Nothing writes a region measure from this yet; that is pocket
  slice 2.

## Flow-chamber boundary conditions ride this mechanism

A `flow:` pocket's species boundary conditions are the same patch
measures: `inlet-velocity`/`pressure-drop` (the operating point),
`species-diffusivity`, `inlet-concentration`, and `wall-rate-constant`
on floor/lid patches (one measure per reactive patch — each is scored
separately). Species scope is the name prefix `<species>/<prop>` matching
the token's `species=` list; a bare name applies to every species.
Unlike the purely declarative properties above, these feed live
computers (`outlet-conversion`, `outlet-concentration`,
`wall-flux-fraction`, `damkohler`), so they are checked, not
`measurand_unchecked`. Chamber, solver routing and the `wall_rate`
catpath binding: `precis-se-flow-help`.

## Why view='drc' says measurand_unchecked

No property computer is live yet. Every measure whose measurand is not
`length`, `count`, `ratio` or `angle` is stored intent: its band is
recorded, nothing computes a value to check it against, and DRC warns
about each one instead of passing it silently. The `realised` column in
`view='pockets'` stays `—` until a computer covers that measurand.

A measure keeps the measurand's taxon id; the slug shown follows a
rename of the taxon node.

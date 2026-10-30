---
id: precis-se-regions-help
family: se
title: precis — region properties on an se design (measurands, region selectors, pockets, observation rays)
summary: declare non-geometric properties on part of a block — "this side hydrophobic, that patch negative" — as ordinary se measures that name a taxonomy measurand (surface charge density, contact angle, net partial charge, dipole moment, H-bond donor/acceptor count, electric field magnitude, absorption maximum wavelength, hydrophobicity index) and a region selector (patch:/ring: on a face, sites:/atoms: on bound atoms); group regions into a named pocket with add_pocket and read them back with view='pockets'. Nothing computes these values yet — view='drc' flags each as measurand_unchecked
answers:
  - how do I say one face or patch of a block is hydrophobic, charged or polar?
  - how do I declare a binding pocket with regions of different charge?
  - what measurands can an se measure use besides metres, counts, ratios and degrees?
  - how do I address part of a face, a rim, a seam span or specific atoms in a selector?
  - why does view='drc' report measurand_unchecked?
  - why is my measurand refused or ambiguous?
  - how do I see where a bound molecule is charged, H-bond donating or accepting, polar or nonpolar?
  - how do I bookmark a line through my design and see which atoms, π rings and pockets it passes?
  - what did the relax change along the pore (observe_ray, view='ray' against='last')?
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
| `frame:<key>` | the atoms a workspace-run trace frame's subtree made — `frame:<key>!` the frame alone, `/rest` the subtree minus the frame, `/touched` its touched ranges | the atoms' ordinals + centroid |
| `pi:<block>#<id>` | π system `id` on the block's bound structure (the spatial eye's `pi:` line names it); `/rings` the system's rings, `/aromatic` only aromatic rings | the system's atom ordinals + centroid |
| `mpatch:<mol>:<id>` | patch id of the molecule block's painted surface — the wall region opposite it | the wall atoms within clearance + 3 Å of the patch's points |
| `mol:<mol>@<clearance Å>` | the molecule's whole surface — the wall region around it | the wall atoms within clearance + 3 Å of any surface point |
| `mcone:<mol>:<atom>@<dx>,<dy>,<dz>/<half°>+<length Å>` | a cone with its apex on one molecule atom, axis in the molecule structure's own frame (it turns with the block's `rot`) | the wall atoms whose centres sit inside the cone |

`<mol>` is a block (`nh3`) or a block minus one atom of its structure
(`nh3!aH3`, the held fragment of pocket_from_molecule's `remove=`).

A pocket's `shape` also accepts `mol:<mol>@<clearance Å>` — the
molecule's surface offset outward by the clearance (default 0;
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
- `frame:<key>` cites a construction-trace frame (slice 2t) — the key
  comes from `get(kind='se', id=..., view='trace')`; keys are stable
  across reruns of an unchanged script, so it needs no version pin. It
  resolves only on a workspace-built structure (a bound ref whose
  `generated.trace` names the run).
- `pi:<block>#<id>` cites a perceived π system (2e); on a bare structure
  the form is `pi:<id>` (`st<id>~pi:<id>`).

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

## pocket_realize

`{'op': 'pocket_realize', 'wall': '<block>', 'pocket'?: '<name>',
'candidates': ['<structure slug>', …], 'cap'?: <int>, 'dft'?: {…}}` — a
handler-level op (needs the store and GFN2): the intent→realize pick →
join → check loop (pocket-from-molecule slice 4). `pocket` is optional
when the wall has exactly one. Each candidate is a `structure` design
posed by the wall block's transform. For each, the loop cuts the wall
atoms within `clearance + 4 Å` of the pocket's molecule surface, H-caps
the severed bonds, places the molecule at its spec pose, relaxes the
complex at GFN2 (wall atoms beyond `clearance + 3 Å` of the surface
pinned), then scores: binding energy (E_complex − E_wall − E_mol), each
region measure against its band on the relaxed wall, and for a
held-shape pocket the fragment's Kabsch-aligned RMSD from its frozen
shape. Ranking: fewer unmet hard measures, then fewer unmet of any
strength, then stronger binding; `cap` bounds the evaluations.

The op writes nothing — the echo is the ranked list: per candidate the
unmet counts, binding energy, relax verdict, cluster/pin counts and
drift, the failing regions of every loser, and whether the best met
every hard band. Every result carries provenance (candidate source,
pocket `class_hash`, iteration); a `dft` mapping (autocatpath's `dft`
config block) rides verbatim into provenance for the slice-5 refinement
ladder. A candidate that cannot even join (no wall atoms in the cut, a
complex over the 160-atom bound) is reported, never silent. It is a
research loop, not a solver: a run can end with no candidate inside
every band.

**No tblite on this host?** (Q13 — the prod MCP image is linux-aarch64,
which PyPI has no wheel for): the op then enqueues an
`se_pocket_realize` job on the compute lane and the echo names the job;
`get(kind='job', id=<job ref>)` shows the run — its `job_summary`
chunk carries the same ranking the op echoes inline, and the
`job_result` chunk holds the full result JSON (capped on the card;
`view='result'` returns it whole). If no compute
node is configured at all the op refuses up front instead of queuing a
job nothing could claim.

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
- Surface settings are fixed (decided 2026-10-25): Bondi radii × 1.0,
  3 points/Å², no probe. `scale` (0.5–3) and `density` (points/Å², up
  to 20) override them for one read.
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

## Observation rays

A ray is a stored **definition** — origin, direction, length, fields —
never an event list. Every read recomputes the events, so a ray stays
valid when the geometry changes under it (multiscale §5.5 bookmarks).
Rays never touch geometry.

### On an se design

```
edit(kind='se', id='<slug>', ops=[{'op': 'add_ray', 'block': 'p',
  'name': 'axis', 'origin': '[0 0 -2] nm', 'direction': [0, 0, 1],
  'length': '12 nm', 'fields': ['pass', 'pi', 'frame'],
  'reach': 1.5}])
get(kind='se', id='<slug>', view='ray', args={'name': 'axis'})
get(kind='se', id='<slug>', view='rays')
get(kind='se', id='<slug>~ray:axis', extent='fisheye')
```

- `origin`/`length` are metres (bare numbers) or unit strings; `step`
  (sampling, default 0.5) and `reach` (how far off the line an atom
  still counts, default 4) are Å.
- `frame`: omitted = the owning block's local frame (the ray moves with
  `set_pose`, instances and arrays); `'world'`; `'port:<name>'` (origin
  at the port, `+z` its axis).
- Ray names are unique per design. Rays live on template blocks.
- `set_ray` updates the definition; `remove_ray` drops it;
  `observe_ray` (`name`, or all) stores a digest (event hash, structure
  versions, key numbers; last five kept).
- `view='ray'` `args={'against': 'last' | <digest index>}` recomputes the
  events at the digest's structure versions and lists hits added,
  removed and moved (`Δs`, `Δd`); unchanged hits fold to a count.
  `args={'from': '14 Å'}` resumes past the 40-hit cap. A transient ray
  takes `block`/`origin`/`direction`/`length` in `args` with no `name`.
- A relax write-back re-observes the rays on the blocks it relaxed, so
  `against='last'` after a relax reads what the relax moved.

### Fields

| field | hit |
|---|---|
| `pass` | an atom within `reach`: `s`, handle, `d` (Å off the line), `∠` to its local normal (POAV for 3-coordinate, bond axis for linear, `—` else) |
| `pi` | `pi.enter`/`pi.leave` an aromatic ring's cylinder (ring plane ± 1.7 Å) or a ring-less conjugated system; carries the `pi:<block>#<id>` selector |
| `frame` | the nearest atom's maker frame changes: `junction#8 (frame:<key> · main.py:30 layout)` |
| `relax` | the nearest atom's per-atom relax level changes (needs slice 4a's levels; absent until then) |
| `charge` | `q` on passes and `esp` hits (needs stored partial charges, slice 4; absent until then) |
| `region` | a block envelope or a shaped pocket entered/left |
| `solid` | macro: the event list below, in the ray's own unit |

A field with no data is absent from the header's `fields` list, never a
per-hit gap. Atomic hits read in Å from the ray origin; macro hits in
nm/µm/mm by ray length; all sort by `s` along the one world ray.

## Observation rays: macro leg (event list, four kinds)

A non-structure block is probed through its **bound cad design** when it
binds one (holes read as holes), else its envelope. Events, in `s` order:
`enter`/`exit <block>` per contiguous run of material (a re-entry is
`<block>#2`; a hole the ray runs straight down is not material and has
none), `void <block>.<feature> ⌀d · len · axis offset · build.py:44 cut#5 ·
frame:<key>` for each carved feature crossed, `gap` between blocks, and
`port.enter <block>.<port> M5x0.8 (role …) · axis offset …` for a port
with pose, direction and a `thread`/`diameter` (m) annotation (`depth`
m sets its length). `kind` adds a report under the events:

| kind | report |
|---|---|
| `bolt_axis` | stack to the threaded port's block, thread depth = its hole (≥ 1.5 d ✓/✗), radial clearance (tightest stack hole), axis offset from the threaded hole, `screw: M5 × 16 · engagement 9.8 mm` — the shortest ISO 4762 length with stack + 1.5 d ≤ L ≤ stack + depth, or `no standard length fits — 12 short by …, 16 bottoms out by …` |
| `tool_access` | `clear ✓` or `blocked by <block> at s ✗`; port cylinders are not counted |
| `load_path` | thinnest material crossing; stress/utilisation need a stored field outcome (absent) |
| `manufacturability` | each solid wall vs the block mode's `min_wall` (holes are not walls); undercut when a block is re-entered along the axis |

`'bundle': {'radius': '4 mm', 'count': 8}` on `add_ray` makes one probe
of `count` parallel rays on that circle, the union of their hits. An
import re-observes every ray: one whose events changed since the
previous version gets `ray.changed` (`view='validate'`), and the import
echo shows the old and new lists.

## Observation rays: bare structure designs and scripts

### On a bare structure design

`edit(kind='structure', id=…, ops=[{'op': 'ray', 'name': 'up',
'origin': [x, y, z], 'direction': [0, 0, 1], 'length': 8}])` — Å
throughout; `view='ray'` `args={'name': 'up'}`, `view='rays'`,
`st<id>~ray:up` on the eye; `{'op': 'unmark', 'name': 'up'}` removes it.
No trace, so `frame` is absent.

### From a workspace script

`pga.ray(name, origin, direction, length, block=…, fields=…,
step=…, reach=…, frame=…)`, `pga.ray.along(geom.port('rim'), '5 nm',
name=…)`, `pga.ray.axis(tube, name='axis')` (principal axis of the
atoms), `pga.ray.bundle(r, '4 mm', 8)` (a tool's tube),
`pga.find_holes(plate)` (proposes fastener ports for M-size cuts:
`thread` at the nominal ⌀, `clearance_for` within ISO 273 fine–coarse);
`out.ray(r)` writes the manifest `rays:` entry. The import makes
each a block child with a `made-by:` line naming the script frame; a ray
the next run stops writing retires. `pg`/`pga`/`out` are the import
aliases every workspace example uses — the whole loop is in
[[precis-workspace-help]].

## Ad hoc probes — view='inspect'

One probe family at Å and macro scale, computed on demand and never
stored (stored rays stay `view='ray'`). `probe=` names what comes back:

| probe | answers |
|---|---|
| `ray` | material runs along a line, with the surface frame at each end; `profile_samples` adds the free radius and the frame that pinches it (a channel bottleneck); `radius` sweeps a cylinder — blocked or clear; `events` merges `pi`/`frame` hits |
| `section` | the loops a plane cuts, `hole_count`, wall distance, bonds crossing the plane |
| `overlap` | intersection volume with `with=<selector>` (another frame of the block) or a `radius` cylinder, naming the frames on both sides |
| `thickness` | face-on column map, one ray per `pitch` node (slow below 0.5 Å) |

```
get(kind='se', id='<slug>', view='inspect', args={'block': 'p',
  'probe': 'ray', 'origin': '[0 0 -1] nm', 'axis': [0, 0, 1],
  'profile_samples': 200, 'radius': '0.4 nm'})
get(kind='se', id='<slug>', view='inspect', args={'block': 'p',
  'probe': 'overlap', 'target': 'frame:<tube>', 'with': 'frame:<guest>'})
get(kind='structure', id='<slug>', view='inspect',
  args={'probe': 'section', 'normal': [0, 0, 1]})
```

`origin`, `pitch`, `radius`, `length`: metres or unit strings on se,
bare Å on structure; `axis`/`normal` are directions. Omitted, the origin
is the target's centroid (a ray starts 2 Å before it) and the direction
its principal axis. `target` narrows to `frame:<key>`, `pi:<id>`,
`find:element=C` or atom labels (se only for `frame:`). Atoms are
van der Waals spheres (Bondi; `radii={'C': 0.9}` overrides), so a benzene
ring-plane section is one loop — the ring centre is inside the carbon
spheres. A macro block probes its bound cad design or envelope, in mm.
From a script: `pg.inspect.ray/section/overlap/thickness(geom, …)` — a runnable guest-fit check is in [[precis-workspace-mol-help]].

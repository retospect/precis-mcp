---
id: precis-se-chain-help
title: precis — nucleic-acid chains in se (DNA/RNA helices, strands, domains)
summary: six pure ops declare a helix (geometry), a strand (route chemistry) and its route (add_domain/remove_domain) over an ordinary se block tree, then materialise the helix's swept tube (layout_chain) or un-declare it (clear_chain); pairing is DERIVED from two strands occupying one helix offset running opposite ways, never declared; view='chain' + ten chain_* DRC findings check it
answers:
  - how do I declare a DNA/RNA helix and route a strand along it in se?
  - how do I make a crossover, a hairpin loop, a foothold/toehold in se?
  - why is pairing derived instead of declared — how do two strands actually pair?
  - what do the chain_bend/chain_clash/chain_loop_short/... findings mean and how do I fix them?
  - how do I materialise a helix's segments so the 3D viewer draws it (layout_chain)?
  - what Leontis-Westhof geometries can geometry=/overrides= take and which bases do they accept?
  - why does declare_helix/add_domain reject a bare number?
applies-to: put/edit (kind='se', op=declare_helix|declare_strand|add_domain|remove_domain|clear_chain|layout_chain)
status: active
tags: verbs, design
kinds: se
---

# precis-se-chain-help — nucleic-acid chains in se

The decomposition (scadnano's): a **helix** block carries the GEOMETRY
(centre line, motif, unit count, register); a **strand** block carries the
route's chemistry (sequence, optional) and routes over helices via ordered
**domains**; a **loop** is not its own thing to declare — it is the
`loop_before_nt` gap between two consecutive domains on one strand, and
`0` is a real, common answer (a crossover). **Pairing is never declared** —
route two strands over the same helix offsets running opposite directions
and the pair falls out of that; see "Pairing is derived" below, the single
most important idea here.

These six ops sit on an ordinary se block — `declare_helix`/`declare_strand`
mark a block `add_block` already minted, the same way `set_mode`/`declare_dof`
do. For the rest of the `se` call surface (blocks, ports, connects, measures,
BOM, states) see [[precis-se-help]].

## Declaring a helix — `declare_helix`

`block=` (existing block) + `n_units=` (base pairs, req, ≥1) + a centre
line: either `lattice='honeycomb'|'square'` + `row=`/`col=` (a straight
helix on a lattice site) **or** `path={'waypoints': [[x,y,z], …]}` (a free
centre line, every coordinate a length with a unit; ≥2 points). Optional:
`nucleic='DNA'|'RNA'` (default `DNA`) or an explicit `motif=`; `phase0=`
(an angle, unit required — the register offset of unit 0); `register=
{'lattice': …}` (defaults to the path's own lattice site; `insertions`/
`deletions` are a reserved hook with no consumer yet — refused when
non-empty, never silently stored and ignored); `min_bend_radius=` /
`min_gap=` (lengths, unit required). Authoring `min_bend_radius` promotes
`chain_bend` from warn to error — an explicit limit is the design's own
claim, not a coded guess.

```python
edit(kind='se', id='design', ops=[
    {'op': 'add_block', 'name': 'stem'},
    {'op': 'declare_helix', 'block': 'stem', 'n_units': 21,
     'lattice': 'honeycomb', 'row': 0, 'col': 0,
     'min_bend_radius': '12 nm', 'min_gap': '0.6 nm'},
])
```

Re-declaring an existing helix is a full replace — any `layout_chain`
children it already has are dropped, since a stale tiling would still
*look like* a seam a realizer trusts.

**Every length and angle needs a unit; plain counts and offsets don't.**
`n_units`/`row`/`col`/`start`/`end`/`ord` are bare integers — no unit, and
none accepted. `min_bend_radius`/`min_gap`/`phase0`/every waypoint
component/`max_seg_len` are quantities: `12` raises `UnitRequiredError`
("state units: '12 nm'? '12 m'?"); `'12 nm'` or `'12 nm'`/`'1.57 rad'`
works. A bare `0` coordinate is the one exception (nothing to
disambiguate at the origin).

## Declaring a strand — `declare_strand`

`block=` (existing block) + optional `sequence=` and `nucleic=` (default
`DNA`). Sequence is optional on purpose — a 24-helix rectangle is a real
design long before anyone has picked its 7 kb, and every letter-dependent
check reports "unverifiable" rather than inventing bases. Letters: `A C G
T N` for DNA, `A C G U N` for RNA (`N` = explicit don't-know); anything
else is refused, never silently dropped. `T`/`U` are folded together for
pairing-geometry purposes only — the stored sequence keeps the letter you
wrote.

Replace semantics, but existing domains are **not** dropped — a route
survives its sequence being filled in later, the ordinary order of work.
The route itself is `add_domain`, never part of this op.

## Routing a strand — `add_domain` / `remove_domain`

`add_domain`: `strand=` + `helix=` (must be different blocks — a strand
routes *along* a helix, not onto itself) + `start=`/`end=` (helix offsets,
half-open `[start, end)`, plain integers, `end > start`) + `forward=`
(bool, required, no default — which way the strand runs through the
offsets). Optional `loop_before_nt=` — the unpaired gap between the
*previous* domain's 3' exit and this one's 5' entry; **`0` is a real
answer** (one backbone bond of reach — feasible only at a register-correct
offset, which is exactly what `chain_loop_short` at `n=0` checks). Refused
on a strand's first domain (`ord == 0`) — there is no preceding exit to
reach from; a 5' overhang is its own domain, not a loop. `geometry=` names
a Leontis–Westhof family for the whole domain; `overrides={offset:
geometry}` overrides individual offsets inside it (2026-09-28 decision:
per-position geometry is an override, never its own 1-bp domain).

Appends at the end of the strand's 5'→3' route — `ord` is **assigned**
(`len(existing route)`), never accepted, so a route can never grow a hole.

```python
edit(kind='se', id='design', ops=[
    {'op': 'add_block', 'name': 'hp'},
    {'op': 'declare_strand', 'block': 'hp', 'sequence': 'GGGGAAAACCCC'},
    {'op': 'add_domain', 'strand': 'hp', 'helix': 'stem',
     'start': 0, 'end': 4, 'forward': True, 'geometry': 'WC'},
    {'op': 'add_domain', 'strand': 'hp', 'helix': 'stem',
     'start': 0, 'end': 4, 'forward': False, 'loop_before_nt': 4,
     'overrides': {'2': 'W-W-cis'}},
])
```

A **crossover** is a `loop_before_nt=0` domain onto a *different* helix —
0-nt "loop", one bond of reach, which is why it only lands at a
register-correct offset (the two backbones must face each other within
that one bond). `chain_loop_short` at `n=0` **is** the register check for
a crossover, not a separate thing.

`remove_domain` — `strand=` + `ord=`. **Destructive** by the `remove_`
prefix rule (a human-Apply proposal in the web turn, like `remove_block`).
The rest of the route re-indexes to close the gap, and the domain that
becomes the new 5' end loses its own `loop_before_nt` (nothing left to
reach from). Removing the strand or helix block itself (`remove_block`)
takes every domain that names it along too, and closes up whatever
survives.

## Pairing is derived, never declared

Route two strands over the **same helix offsets** with opposite `forward`
flags (one `True`, one `False`) and `precis_se.chain.pairing.derive_pairing`
finds them **paired** there — nothing anywhere states "these two bases
pair". One occupant at an offset = **single-stranded** (a foothold,
toehold, or ssDNA scaffold overhang — all the same thing at this
altitude). Two occupants running the **same** way, or three or more, is a
`chain_occupancy` **error** (parallel duplexes and triplexes are reported,
not modelled — out of scope for this cut). When a domain's `geometry`/
`overrides` disagree about which family governs one offset, the first
domain to declare it (by strand/ord order) silently wins — nothing
flags the disagreement today.

`view='chain'` shows the tally per helix (`N paired · M single · K free`,
`+J CONFLICT` when any occupancy error exists) and the single-stranded
runs; `view='drc'` is where the occupancy/geometry findings actually
surface.

## Materialising segments — `layout_chain`

`block=` one helix, or omit it to run over every helix the design
declares (error if it declares none) — + optional `max_seg_len=` (a
length; default is **one lattice repeat**: 21 units honeycomb, 32 square,
or 21 for a free-path helix with no lattice).

Mints child blocks `<helix>.s<k>`, one per tiled unit range: a `cyl`
envelope from the kernel's capsule pose (a `sphere` for a single-unit
segment — a zero-length capsule isn't a cylinder), ports `5p`/`3p`
(anchors for a realizer/relax pass, not connect targets — skipped by
`unconnected_port`), and its own `chain` record naming the inclusive
`[start, end]` unit range it covers. **The ranges tile the helix exactly**
— no gap, no overlap — which is what a downstream realizer needs to find
the segment covering any offset. Both the envelope and the pose are
stamped `origin='proposed'`.

```python
edit(kind='se', id='design', ops=[{'op': 'layout_chain', 'block': 'stem'}])
edit(kind='se', id='design', ops=[{'op': 'layout_chain', 'max_seg_len': '7 nm'}])  # every helix
```

**Re-running retires and regenerates** — a segment is derived, never
authored; the same happens automatically when you re-`declare_helix` the
parent. Never hand-edit a `.s<k>` block. These children are what the 3D
viewer actually draws — a helix with no `layout_chain` run has geometry
(a centre line) but nothing rendered.

`layout_chain` on a non-helix block is refused ("declare_helix it first —
a strand has no geometry of its own").

## Clearing a declaration — `clear_chain`

`block=` — un-declares a block's `chain` record, cascading to whatever it
gave meaning: on a **helix**, its `layout_chain` children and every
domain routed along it go too (surviving routes on the same strand
re-index); on a **strand**, its whole route goes. Not destructive by the
naming rule (like `clear_dof`/`clear_build_frame`) — it's redoing a
declaration, not undoing prior work.

## Leontis–Westhof geometries — `geometry=`/`overrides=`

12 canonical families, spelled `<edge>-<edge>-<orientation>` — edges `W`
(Watson–Crick) `H` (Hoogsteen) `S` (Sugar), orientation `cis`/`trans`, the
edge pair unordered (`H-W-cis` canonicalises to `W-H-cis`). Shorthand:
`WC` / `watson-crick` / `wobble` → `W-W-cis` (a G·U wobble is
geometrically Watson–Crick-edge cis; only the base combination differs),
`hoogsteen` → `W-H-cis`, `reverse-hoogsteen` → `W-H-trans`, `sugar` →
`W-S-cis`, `mismatch` → `W-W-trans`.

`chain_pairing_geometry` (error) fires when a paired offset's two letters
aren't in the declared family's **curated** occupancy table (Leontis &
Westhof 2001 + the 2002 isostericity matrices — the frequent occupants,
not an exhaustive enumeration; a family with no coded occupants accepts
nothing). An unsequenced letter, or `N`, is unverifiable and never flags.

## Views

- `view='chain'` (no `args`) — every **helix**: motif (`B-DNA`/`A-RNA`,
  retuned per lattice), unit count, turns, lattice site, segment tiling,
  occupancy tally. Every **strand**: total nt (domains + loops), sequence
  length (or `(none)`), its domain route, its loops. Then the **derived
  pairing** summary: paired/single/conflict counts and every contiguous
  single-stranded run with its nt count and contour.
- `view='topology'` gains a "## domains" table (`strand · ord · helix ·
  offsets · dir · loop_before · geometry`) for every `add_domain` row —
  pairing and geometry checks live in `view='chain'`, not here.

## DRC — the ten `chain_*` findings

| rule | tier | fires when | fix |
|---|---|---|---|
| `chain_bend` | warn / **error** | centre line bends tighter than `min_bend_radius` (coded default Lp/5 ≈ 10 nm for B-DNA; error when authored) | lengthen the run, relax the waypoints, or accept the authored limit |
| `chain_twist_register` | warn | `n_units` isn't a whole number of the lattice's repeat, rolled back into register | an insertion/deletion, or change `n_units` |
| `chain_clash` | warn | two segments' swept tubes closer than `min_gap` (coded default: low-end 2.4 nm spacing minus 2× the motif radius — 0.4 nm B-DNA) — consecutive same-helix segments and a crossover's own two segments are exempt | move a helix, or widen `min_gap` |
| `chain_loop_short` | **error** | a loop's `(n+1)·contour_per_nt` can't bridge its two backbone exits — **this is the crossover register check at `n=0`** | more nt, or a register-correct offset |
| `chain_loop_slack` | info | the opposite — a loop with far more contour than it needs | fewer nt would pin the geometry |
| `chain_floppy` | info | a single-stranded span (or loop) past ssDNA's coded persistence length (2 nm) | coded default only here — a `material` Lp row overrides it in the handler-side pass, not this one |
| `chain_dangling_domain` | **error** | a domain names a gone/wrong-role block, an offset past the helix's `n_units`, or a route whose `ord`s have a hole/repeat | `declare_strand`/`declare_helix` it, extend the helix, or `remove_domain` the row |
| `chain_occupancy` | **error** | two parallel occupants, or 3+, at one offset | reverse a domain's `forward`, or move it |
| `chain_pairing_geometry` | **error** | declared family doesn't accommodate the two paired letters (per the curated table) | change the bases, or the declared geometry |
| `chain_malformed` | **error** | a stored `chain` record doesn't fit the schema at all (hand-corrupted) | defence in depth — never crashes the read path, but the record needs fixing |

## Not built yet

`relax_chain` (a mechanical settle — segment rigid bodies + hinges + loops
+ crossover pins) and `fold_layout` (ViennaRNA dot-bracket → ops) are
handler-level ops behind an optional dependency and are **not live** —
don't reach for them. Neither are their findings (`chain_fold_disagree`,
`chain_offtarget`, `chain_fold_unavailable`), a `material`-Lp re-emission
of `chain_floppy`, `realize_chain`/atoms-per-region, or any
scadnano/caDNAno/oxDNA/PDB export. Everything above — the six ops, the
two views, the ten findings — is the whole live surface.

## See also

- [[precis-se-help]] — blocks, ports, connects, measures, BOM: the rest of
  the `se` call surface a chain design's blocks still use.

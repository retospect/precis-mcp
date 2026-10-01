---
id: precis-se-chain-help
title: precis — nucleic-acid chains in se (DNA/RNA helices, strands, domains)
summary: seven pure ops declare a helix (geometry), a strand (route chemistry) and its route (add_domain/set_domain/remove_domain) over an ordinary se block tree, then materialise the helix's swept tube (layout_chain) or un-declare it (clear_chain); pairing is DERIVED from two strands occupying one helix offset running opposite ways, never declared; view='chain' + fifteen chain_* DRC findings check it; three handler-level proposals finish the job — relax_chain settles the segments and stores each placed loop's curve, fold_layout turns a ViennaRNA MFE fold into helix/strand/domain records, realize_chain mints Arnott B-DNA fibre atoms for one region as a bound structure design; view='export' writes the design out as scadnano/caDNAno/oxDNA/PDB; walker states: see precis-se-walker-help
answers:
  - how do I declare a DNA/RNA helix and route a strand along it in se?
  - how do I make a crossover, a hairpin loop, a foothold/toehold in se?
  - why is pairing derived instead of declared — how do two strands actually pair?
  - what do the chain_bend/chain_clash/chain_loop_short/... findings mean and how do I fix them?
  - how do I materialise a helix's segments so the 3D viewer draws it (layout_chain)?
  - what Leontis-Westhof geometries can geometry=/overrides= take and which bases do they accept?
  - which offsets admit a 0-nt crossover on a honeycomb/square lattice (the register rule)?
  - how do I move one crossover by a base pair without rebuilding the strand (set_domain)?
  - how do I settle a chain design's geometry, and what does relax_chain NOT do (relax_chain)?
  - how do I turn a sequence's ViennaRNA fold into helices/strands/domains (fold_layout)?
  - why does view='drc' say chain_fold_unavailable, and what is chain_offtarget telling me?
  - why does declare_helix/add_domain reject a bare number?
  - how do I export a chain design to scadnano/caDNAno/oxDNA/PDB (view='export')?
applies-to: put/edit (kind='se', op=declare_helix|declare_strand|add_domain|set_domain|remove_domain|clear_chain|layout_chain|relax_chain|fold_layout|realize_chain)
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

All ten ops sit on an ordinary se block — `declare_helix`/`declare_strand`
mark a block `add_block` already minted, the same way `set_mode`/`declare_dof`
do (`fold_layout` is the exception: it mints the blocks its fold needs). Seven
are pure and auto-apply; `relax_chain`, `fold_layout` and `realize_chain` are
handler-level and arrive as proposals. For the rest of the `se` call surface
(blocks, ports, connects, measures, BOM, states) see [[precis-se-help]].

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

## Routing a strand — `add_domain` / `set_domain` / `remove_domain`

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
register-correct offset (both backbones must face the other helix).
`chain_loop_short` at `n=0` **is** the register check for a crossover, not
a separate thing; see "The register rule" below for which offsets those are.

`set_domain` — edit ONE existing domain in place. `strand=` + `ord=` select
the row; then any of **`helix`, `forward`, `start`, `end`, `geometry`,
`overrides`, `loop_before_nt`** changes it. Absent means unchanged; passing
one explicitly as `null` clears it. Unknown keys are refused rather than
ignored, and `ord` is **not** settable (it identifies the row — the route's
order belongs to `add_domain`, which appends, and `remove_domain`, which
closes the gap). Everything `add_domain` refuses, this refuses too, e.g. a
`loop_before_nt` on `ord == 0`.

Pure, so it runs in the design turn's dry run with no human Apply — which is
the point: moving a crossover a base pair along is one op, not
`clear_chain` plus a re-route of every strand involved.

```python
edit(kind='se', id='design', ops=[
    {'op': 'set_domain', 'strand': 'st0', 'ord': 1, 'start': 8, 'end': 16},
])
```

`remove_domain` — `strand=` + `ord=`. **Destructive** by the `remove_`
prefix rule (a human-Apply proposal in the web turn, like `remove_block`).
The rest of the route re-indexes to close the gap, and the domain that
becomes the new 5' end loses its own `loop_before_nt` (nothing left to
reach from). Removing the strand or helix block itself (`remove_block`)
takes every domain that names it along too, and closes up whatever
survives.

## The register rule — which offsets admit a 0-nt crossover

A duplex's two backbones are **not** on opposite sides of the helix: they sit
δ apart across the minor groove — **144° for B-DNA**, 220.5° for A-RNA (the
A form's minor groove is the wide one), symmetric about the frame's normal, so
`phase0` points the base pair's own pseudo-dyad and *not* a backbone. A 0-nt
crossover therefore needs the departing backbone pointed at the neighbour and
the arriving one pointed back, which happens where

> `phase0 + offset × twist ≡ neighbour_azimuth ± π/2` (mod 2π), within ±10.9°
> — **`+π/2`** for a `forward → reverse` crossover, **`−π/2`** for
> `reverse → forward`.

The two directions are **different offsets**, half a turn apart. With every
helix on the same `phase0` (the usual authoring), `twist` the lattice's own
(3 turns/32 bp square, 2 turns/21 bp honeycomb), the answers are:

| lattice | neighbour | `forward → reverse` | `reverse → forward` |
|---|---|---|---|
| honeycomb (per 21 bp) | `col+1` | **14** | 9, 19 |
| | `col−1` | **7** | 2, 12 |
| | `row−1` | **0** | 5, 16 |
| square (per 32 bp) | `col+1` | 24 | 8 |
| | `row+1` | 16 | 0 |
| | `col−1` | 8 | 24 |
| | `row−1` | 0 | 16 |

Read it this way: **one offset per neighbour per lattice repeat.** Aggregated
over a site's 3 (honeycomb) or 4 (square) neighbours that is one crossover
site every 7 or 8 bp — the lattice's documented crossover period. The
honeycomb's forward→reverse column, 0/7/14, is caDNAno's own honeycomb
crossover positions; its reverse→forward offsets have no exact solution and
sit ±5 bp either side, reachable but strained.

Shift `phase0` and the whole table shifts with it: add `Δ` to a helix's
`phase0` and its register-correct offsets move by `−Δ/twist`. If you want
offset `k` to work for a given neighbour, set
`phase0 = neighbour_azimuth ± π/2 − k × twist`.

When a crossover misses, `chain_loop_short` reports the gap **in nm** and
names the landing offsets on the target helix that would reach — the answer,
not just the complaint.

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
`unconnected_port`; each carries the forward strand's backbone-exit
pose at the segment's first/last unit and faces outward along the axis,
so a helix end is a `view='stations'` target or a distance endpoint
before any atoms exist), and its own `chain` record naming the inclusive
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

An **undeclared** duplex claims strict Watson–Crick complementarity:
`chain_pairing_mismatch` (error) fires on any co-occupied offset whose two
letters don't complement (A·G, G·T, …) — a G·T wobble is a declared
`W-W-cis`, never an undeclared pair. `view='chain'`'s derived-pairing
section tallies every pair's letters (`N complementary · M MISMATCHED ·
K unverifiable` — strict Watson–Crick for an undeclared pair, the curated
table for a declared family), so "do these base pairs in fact match" is a
read, not a look at the render.

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
  pairing** summary: paired/single/conflict counts, the letters tally
  (complementary / MISMATCHED / unverifiable — each pair judged as
  `chain_pairing_mismatch`/`chain_pairing_geometry` would), and every
  contiguous single-stranded run with its nt count and contour.
- `view='topology'` gains a "## domains" table (`strand · ord · helix ·
  offsets · dir · loop_before · geometry`) for every `add_domain` row —
  pairing and geometry checks live in `view='chain'`, not here.
- `view='export'` (`args={'format': 'scadnano'|'cadnano'|'oxdna'|'pdb'}`) —
  the design written out to one of the four interop formats; see
  "Export" below.
- `view='pick'` — the residue, base pair, domain, strand and blocks one
  realized atom belongs to, a citable `<se:…>` token per level; see
  [[precis-se-chain-atoms-help]].

## DRC — the fifteen `chain_*` findings

| rule | tier | fires when | fix |
|---|---|---|---|
| `chain_bend` | warn / **error** | centre line bends tighter than `min_bend_radius` (coded default Lp/5 ≈ 10 nm for B-DNA; error when authored) | lengthen the run, relax the waypoints, or accept the authored limit |
| `chain_twist_register` | warn | `n_units` isn't a whole number of the lattice's repeat, rolled back into register | an insertion/deletion, or change `n_units` |
| `chain_clash` | warn | two segments' swept tubes closer than `min_gap` (coded default: low-end 2.4 nm spacing minus 2× the motif radius — 0.4 nm B-DNA) — consecutive same-helix segments and a crossover's own two segments are exempt | move a helix, or widen `min_gap` |
| `chain_loop_short` | **error** | a loop's `(n+1)·contour_per_nt + tol` can't bridge its two backbone exits — **this is the crossover register check at `n=0`** (`tol` = 0.098 nm between two B-DNA helices, the groove-asymmetry shortfall no routing can remove; 0 within one helix) | more nt, or a register-correct offset — the finding names them |
| `chain_loop_slack` | info | the opposite — a loop with far more contour than it needs | fewer nt would pin the geometry |
| `chain_floppy` | info | a single-stranded span (or loop) past ssDNA's persistence length — the coded 2 nm, or the design's own `material` `persistence_length` row when it has one | one row per span either way: the handler-side pass **replaces** the coded rows rather than adding to them, and names the row's conditions |
| `chain_dangling_domain` | **error** | a domain names a gone/wrong-role block, an offset past the helix's `n_units`, or a route whose `ord`s have a hole/repeat | `declare_strand`/`declare_helix` it, extend the helix, or `remove_domain` the row |
| `chain_occupancy` | **error** | two parallel occupants, or 3+, at one offset | reverse a domain's `forward`, or move it |
| `chain_pairing_mismatch` | **error** | an offset with no declared geometry is co-occupied by two letters that aren't Watson–Crick complements (A·G, G·T, …; `N`/unsequenced never flags) | change one sequence, or declare the geometry (a wobble is a `W-W-cis`) |
| `chain_pairing_geometry` | **error** | declared family doesn't accommodate the two paired letters (per the curated table) | change the bases, or the declared geometry |
| `chain_pairing_disagree` | **error** | two domains at one offset declare *different* families (`geometry`/`overrides`) — the first-declaring one still wins for every check, this just says the second said something else | `set_domain` an `overrides[offset]` naming the winner, or align both declarations |
| `chain_malformed` | **error** | a stored `chain` record doesn't fit the schema at all (hand-corrupted) | defence in depth — never crashes the read path, but the record needs fixing |
| `chain_fold_disagree` | warn | a strand's own route declares self-pairs its ViennaRNA MFE fold doesn't make (or the fold makes pairs the route doesn't) — named in sequence offsets, 0-based | change the sequence, or the route; a DNA design is folded under RNA parameters, so read it as a hint |
| `chain_offtarget` | warn | ≥ 8 nt of complementarity between two stretches nothing routes as a pair (a 6-mer hash index over every strand, so intended duplexes are subtracted) | redesign one stretch, or accept it; long designs have chance 8-mers and the row count is the figure |
| `chain_fold_skipped` | info | a sequenced strand is longer than 200 nt, so no fold ran for it (`RNA.fold` is O(n³)) — its length is named | fold it deliberately with `fold_layout`, which takes scaffold length |
| `chain_fold_unavailable` | info | ViennaRNA isn't installed, so no fold check ran at all — **one row for the design**, not one per strand | install the `[chain]` extra; `chain_offtarget` needs no library and still runs |

## `relax_chain` — settling the geometry

One handler-level op, so it never auto-applies: it arrives as a
**proposal** you Apply, because it spends compute and reads the store.

`{'op': 'relax_chain'}` settles every helix's `layout_chain` children as
rigid bodies: a welded hinge between consecutive segments of one helix at
the worm-like-chain stiffness `Lp / (2 · segment length)`, a one-sided
spring per loop pulling its two backbone exits together to the loop's own
`(n+1)·contour` (a 0-nt crossover included — one bond of reach), hard pins
on every segment it may not move, and excluded volume at the same
`min_gap` `chain_clash` measures. Optional `move=` (`'all'`, or a list of
helix/segment block names) and `iters=` (FIRE steps, default 500, max
5000).

What it writes, all in one revision:

- each moved segment's **pose**, stamped `origin='proposed'`. A segment
  whose pose is user contract (origin unset) does **not** move — it is
  hard-pinned instead, so your placement constrains the settle — until you
  authorise it by name with `move=`. A name that cannot move is refused,
  never ignored.
- each placed loop's sampled curve on its domain row's `meta.loop_curve`,
  points in metres, computed from the **settled** exits. A domain row with
  no `loop_curve` is one with no placed loop — that is the distinction, so
  an empty list is never stored. The curve leaves a mid-helix exit
  sideways (a crossover runs straight across); an exit on the helix's
  **terminal** unit also carries the helix axis, so a hairpin or a tail
  caps the helix end instead of bowing out flat in the last pair's plane.
- with `state=`, a **station settle** instead: no `loop_curve` write, and
  the settled pose goes to the named state's own pose slot, not the
  tree — [[precis-se-walker-help]].

The summary line names the persistence length per helix and where it came
from: the coded default (B-DNA 50 nm), or the design's own `material`
`persistence_length` row with its conditions. Those are different answers
and the echo never hides which one ran.

**A mechanical settle — not thermodynamics, not sampling, and with no
topology detection.** It finds a low-energy configuration *near the one it
was handed*: a design that starts threaded wrongly stays threaded wrongly,
a loop spring will pull a loop straight through a helix and nothing
notices, and the excluded volume is a penalty rather than a constraint (a
taut loop settles its two capsules ~1% interpenetrated). Read
`view='drc'`'s `chain_clash` on the settled design; the settle is a
proposal about placement, never a verdict on correctness.

Refuses, before writing anything: a helix with no `layout_chain` children
(it never lays one out silently, and never skips one silently either), a
design with no helices, and a design where every segment is user contract
and no `move=` was given.

A **walker** — a rigid body tethered to legs that step along the track
through declared foothold-occupancy states — has its own skill:
`declare_strand(anchor=, tether_nt=)`, `declare_states(occupancy=)`, the
`declare_stations` sugar op, `relax_chain(state=...)`'s station settle and
the state-aware reads are all in [[precis-se-walker-help]], not here.

## `fold_layout` — a ViennaRNA fold as records

The second handler-level op, so also a **proposal**: it needs the optional
`[chain]` extra (no ViennaRNA → `Unsupported` naming the install) and the
fold is O(n³).

`{'op': 'fold_layout', 'strand': 'hp', 'sequence': 'GGGGAAAACCCC'}` folds
the sequence (`nucleic='DNA'|'RNA'`, `parent=` for the minted blocks) and
writes the records that fold IS: **one helix block `<strand>.h<k>` per
stack** of the MFE structure, two antiparallel domains of the strand on
each, the unpaired stretches between consecutive domains as
`loop_before_nt`, and **one single-occupancy stub helix `<strand>.t5` /
`.t3` per unpaired tail**, carrying the strand's first/last domain (a tail
is a domain on a helix nobody else occupies — `view='chain'` counts it
single-stranded, and that is how a realizer finds it). The strand block is
minted if absent, or its own `declare_strand` sequence is used when you
pass none. `GGGGAAAACCCC` → `((((....))))` → one 4 bp helix, two domains,
one 4-nt loop, and `view='chain'` reports four `W-W-cis` pairs.

**The placement is NOMINAL** — because a dot-bracket says what pairs, not
where anything is: each helix straight along `±z`; a helix the strand
reaches through at least one unpaired nucleotide on every crossing sits
`2.5 nm` aside; a helix reached through **zero** unpaired nucleotides on
some crossing (a bulge, a one-sided internal loop, a coaxial stack in a
multiloop, a tail stub) is placed **end to end** on the helix it stacks on,
continuing its axis with `phase0` chosen so the two backbone exits meet —
that crossing is a `loop_before_nt` of 0 and reads as a backbone step, not
a crossover, so `chain_loop_short` stays quiet on it; a bulge is one extra
rise of axial offset per bulged nucleotide. A helix end stacks on ONE
neighbour: a further 0-nt crossing onto an end already taken stays a
crossover, and the summary line says so. Run `layout_chain` then
`relax_chain` to settle it; until you do, the loop geometry is arbitrary and
`chain_loop_short` will say so for the loops that have length.

**Covered shapes**: every pseudoknot-free fold — a hairpin, a multiloop, any
nesting of them, bulges, internal loops, coaxial stacks, unpaired tails.
**Refused by name** (a wrong layout is worse than none): a fold with no
pairs; a pseudoknotted dot-bracket (ViennaRNA's MFE never makes one); a
strand that already routes domains (`clear_chain` first); and > 10 000 nt.

**ViennaRNA's parameters are RNA's**, with `T` read as `U` — a DNA fold here
is an approximation, and every message that carries one says so.

## `realize_chain` — atoms for a region

The third handler-level op, also a **proposal**: Arnott B-DNA fibre atoms
for one region of a laid-out helix, bound as a `structure` design on the
covering segment. Its arguments, ports, loop relax and `envelope_fit`
behaviour are in [[precis-se-chain-atoms-help]].

## Export

`view='export'`, `args={'format': 'scadnano'|'cadnano'|'oxdna'|'pdb'}` —
the only accepted arg (`precis_se.chain.export`). One direction, no
import.

- **scadnano** — helix index = declaration order; `grid`/`grid_position`
  from the lattice, or `grid: none` + a position in nm off a free path. A
  loop with `n > 0` nt becomes a scadnano *loopout*; the longest strand is
  flagged `is_scaffold`.
- **cadnano** (legacy c2) — lattice designs on **one** lattice only; a
  waypoint-path helix is `Unsupported` naming it. Every vstrand is padded
  to the lattice repeat (32 bp square, 21 honeycomb); a loop's nucleotides
  become a caDNAno insertion (`loop[offset] = n`) at the exit base;
  longest strand → `scaf`, every other strand → `stap`.
- **oxdna** — one `## <design>.top` section then `## <design>.conf`,
  nucleotides per strand listed 3'→5'. An unsequenced nucleotide writes
  `N` (oxDNA needs a real base — this is a geometry export, not a
  runnable input). Loop positions follow the placed curve when
  `relax_chain` wrote one, else the chord between the two exits;
  positions in oxDNA length units (0.8518 nm). A starting configuration,
  not an equilibrium one.
- **pdb** — every `realize_chain`-bound segment, world-posed, one chain
  id per segment, `TER` between. `Unsupported` when nothing is realized
  yet. `get(kind='structure', id=<slug>, view='pdb')` on one minted
  structure writes just that region, with residue names.

**Helix indices/row/col are ours, not caDNAno's.** The register walk here
is reflected relative to caDNAno's own (a handedness convention) and
unsettled until compared against a real caDNAno file — don't claim
column-for-column agreement.

## See also

- [[precis-se-chain-atoms-help]] — `realize_chain` in full and
  `view='pick'`: atoms for a region, and what each atom belongs to.
- [[precis-se-help]] — blocks, ports, connects, measures, BOM: the rest of
  the `se` call surface a chain design's blocks still use.
- [[precis-se-walker-help]] — DNA walkers: foothold-occupancy states,
  station settles, per-state poses.

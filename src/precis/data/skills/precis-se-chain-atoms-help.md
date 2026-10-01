---
id: precis-se-chain-atoms-help
title: precis — atoms for a nucleic-acid chain region in se (realize_chain, view='pick')
summary: realize_chain mints Arnott B-DNA fibre atoms for one region of a laid-out helix as a bound structure design on the covering segment (loops optional, loop backbone relaxed by default, measured 5p/3p ports, sites= attachment ports); view='pick' resolves one atom of a realized region to its residue, base pair, strand domain, strand and blocks, one citable uid-keyed token per level, and reads any token back
answers:
  - how do I get atoms for a region of a helix, and what happens to an unrelaxed loop (realize_chain)?
  - which ports does realize_chain mint on the segment, and how do I add attachment sites?
  - why does envelope_fit skip loop nucleotides, and how is a protruding atom named?
  - which base pair, domain and strand does this atom belong to (view='pick')?
  - what do the <se:…> tokens mean and how do I read one back?
applies-to: put/edit (kind='se', op=realize_chain) plus get(kind='se', view='pick')
status: active
tags: verbs, design
kinds: se
---

# precis-se-chain-atoms-help — atoms for a chain region

The atom half of a chain design. Declaring helices, strands and domains,
`layout_chain`, `relax_chain` and `fold_layout` are in
[[precis-se-chain-help]]; run `layout_chain` (and `relax_chain`, for loops)
before anything here.

## `realize_chain` — atoms for a region

A handler-level op, so a **proposal**: it mints and binds a `structure`
design, so it needs an Apply.

`{'op': 'realize_chain', 'block': <helix>, 'start': <offset>, 'end':
<offset, exclusive>, 'fidelity'?: 'allatom'|'backbone', 'sites'?:
[<offset>, …], 'loops'?: bool, 'relax_loops'?: bool}` — `fidelity`
defaults `allatom`, `loops` defaults `false`, `relax_loops` defaults
`true` (pass `false` to keep the loop's rigid-template placement). One region
per segment child (`layout_chain` first — a
range straddling two segments is refused naming both). Binds to the
**segment child** `<helix>.s<k>` covering the region, structure slug
`<design>-<segment>`; a bound segment is refused
(`set_binding(clear=true)` first).

Atoms are the Arnott B-DNA fibre templates (heavy atoms, no hydrogens)
placed in the motif's own per-unit frames
(`precis_se.chain.atoms.build_region`, called from
`precis_se.atomic.generate.prepare_realize_chain`). An RNA helix is
`Unsupported` — there is no A-RNA template, and B-DNA atoms are never
substituted for one. An unsequenced nucleotide gets backbone atoms only
(residue `DN`); `fidelity='backbone'` keeps P/C4'/C1' for every
nucleotide. `loops=true` also realizes every loop of the region's strands
whose two ends both sit inside the region — a loop with no placed curve
is `Unsupported` naming it (`relax_chain` places curves; nothing here
guesses one); a 0-nt crossover inside the region is just a bond, no
special case. Loop nucleotides are placed as rigid templates at the
curve's own spacing, so their O3'–P steps start at 5–10 Å (connectivity
right, geometry not); `relax_loops` (default) then chains them — a
geometric relax over the loop nucleotides with every duplex atom pinned
(bond springs, repulsion, VSEPR angles: geometry, not thermodynamics, no
pairing or stacking energy), the echo reporting the worst O3'–P step
before and after and the structure's `chain_atoms.loop_relax` keeping the
numbers. The duplex never moves.

Ports minted on the segment: `5p`/`3p` for the first forward-strand chain,
`r5p`/`r3p` for the first reverse one (`5p2`/`r5p2`, … for further
chains), each with a **measured** pose and rot (`bind_structure`'s object
form — `5p` = P with the O5' axle, `3p` = O3' with C3'; backbone fidelity
swaps in C4'). `layout_chain`'s own `5p`/`3p` anchors are kept (their
role, any pose YOU set with `set_port_pose`) and given the same expected
element and `{strand, end, offset, atoms}` annotations as the freshly
minted ports, so the element gate runs for every bound port alike; the
backbone-exit pose layout put there is dropped so the measured atom
fills the slot (`pose_source='bound'`). `sites=[k, …]` adds `n<k>_c5m`/`n<k>_maj`/`n<k>_min`
attachment ports on the forward occupant's base at offset `k` —
underscore, not a dot (a port name can't contain `'.'`) — `allatom`
fidelity and a sequenced base only, both refused rather than
approximated.

Bonds are connectivity only, every one order 1 — a fibre model carries no
bond orders to assign. The echo names atom/bond/nucleotide/chain/port
counts; `view='validate'` then runs `envelope_fit` against the segment's
own capsule — over the duplex atoms only: a loop leaves the tube by
construction, so its nucleotides are skipped, as are inserted bases
(`chain_atoms.residues` rows: helix offset, `null` for a loop
nucleotide, then an insertion index, `i` for `<helix>@<offset>+i`), and
a protruding atom is named as a design object — `aO44 (O3' of DA 8
(stem@3))`.

```python
edit(kind='se', id='design', ops=[
    {'op': 'realize_chain', 'block': 'stem', 'start': 0, 'end': 21,
     'sites': [10]},
])
```

**Measured against the model**: rise 3.34 Å, C1'–C1' 10.7 Å across a
pair, intra-strand P–P 6.26 Å (the model at 10.5 bp/turn — real crystals
give 6.5–7.0 Å), minor/major groove as the shortest inter-strand P···P
distance, 11.9/17.5 Å, right-handed.

## `view='pick'` — every level an atom belongs to

Every level one atom of a realized region belongs to, innermost first,
each row with a token you can cite:
`args={'block': 'stem.s0', 'atom': 44}` (0-based ordinal in the bound
structure) or `'atom': 'aO44'` (the label a finding prints). Rows: atom
("O3' of DG 4") → residue → base pair (`stem@3`, both letters, both
strand domains) → strand domain → strand → segment → helix → ancestors.
A loop nucleotide has no pair row and no domain row.

Tokens key on block **uid**, never a label: `<se:UID#ORD>` atom,
`<se:UID/A.4>` residue (chain.resseq, on the segment), `<se:UID@3>`
offset 3 of a **helix** block, `<se:UID/d0>` domain 0 of a **strand**
block, `<se:UID>` a block. `args={'token': '<se:…>'}` reads any token back
to its row and the levels above it. Works on any block with a bound
structure; without a `realize_chain` record the rows are atom → blocks.

## See also

- [[precis-se-chain-help]] — helices, strands, domains, layout, relax,
  fold, DRC and export.
- [[precis-se-atomic-help]] — bound structure designs in general.

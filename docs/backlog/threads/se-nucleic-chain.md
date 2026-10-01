# se nucleic chain

**Status:** ends when a nucleic-acid design goes from sequence to
orderable strands with an honest atom model (physics-checked, findings that
name design objects) and round-trips caDNAno, reported in the se + hexfold
paper (td344088). Today the chain domain (helix/strand/domain → layout →
relax → realize → export) is landed and dogfooded on prod, and the walker
item is built, deleted, deployed (5ca0cfff on the fleet 2026-09-30 12:23Z)
and dogfooded end to end on prod (`dogfood-walker-a`: guard, channel
budget, crosstalk, make_steps, view='stations' all as specified;
td458169); fold_layout lays out every pseudoknot-free fold (bulges and
coaxial stacks end to end, tails as single-occupancy stubs); loop atoms
chain by default (`relax_loops`, Reto's ruling), envelope_fit skips them,
residue rows persist, and an undeclared pair's letters are checked
(strict Watson–Crick, Reto's ruling); `view='pick'` resolves an atom to
residue, base pair, domain, strand and blocks with a citable token per
level (the resolver half of the pick hierarchy), dogfooded on prod.
The `unpair` op marks one offset not a base pair (one stays at its duplex
position; a run warns as a folding question — Reto, 2026-10-01),
dogfooded on prod. Do-next is blocked.
**Last reviewed:** 2026-10-01 (unpair op shipped)
**Worktree:** `se-nucleic-chain`

## Do next

1. **backlog/se-chain-wrap-around-part.md** — blocked-by
   hexfold-integration; DNA-SE session evidence for the want.

## Horizon

1. **atom findings name design objects** — `envelope_fit` now names its
   atom by residue and helix offset (gr457928's slice); the other
   structure-level findings on a bound segment (bond geometry, clashes)
   still say `aO44` — extend them the same way through
   `atomic/validate.py::chain_atom_name` when one bites in a dogfood.
2. **backlog/ewod-synthesis-protocol.md** (ewod-pcb thread's) — the zone
   compiler that consumes make_steps' make tree (live on prod since
   5ca0cfff); the first end-to-end design → dispense protocol.
3. **backlog/nanostructure-check-tiers.md** §"Chains: the physics tier is
   oxDNA" — oxDNA as a rented relax rung against relax_chain's block-scale
   settle; waits on 1–2 and the oxDNA binary in the image; the only
   honesty check relax_chain has (and the one that would relax the
   stretched step across a deleted base).
4. **caDNAno round trip** (unfiled) — settle the handedness reflection
   against a real file; waits on a file to compare.
5. **backlog/se-chain-staple-sequences.md** — staple assignment beyond the
   scaffold; orderable strand lists.
6. **backlog/se-protein-chain-import.md** — proteins on the same block tree;
   waits on a protein-bearing design being wanted.
7. **td344088** (se + hexfold paper; td345823 next) — reports this arc; the
   walker dogfood (td458169) is its protocol figure's source.

8. **backlog/se-pick-hierarchy.md** — its render half (atom pick on the
   se page, popup, ask-box insertion) is se-3d-viewer's Horizon 1; the
   resolver it calls is built here (`precis_se/pick.py`). Waits on that
   thread's ranking; nothing owed from this side until it starts.

## Parked

- **caDNAno handedness settle** — unparks when a real caDNAno file is
  available; file it then (Horizon 4).

## No action needed

- **insertions/deletions, atoms** — shipped 2026-10-01 in the commit that
  added this line (backlog item deleted): `realize_chain` builds a region
  holding either — a deleted offset has no residue and its neighbours
  bond across it (stretched, the echo prints the step); an inserted
  offset's extras bulge off the duplex and the loop relax chains them;
  residue rows carry an insertion index (`stem@12+1` in pick and
  findings); oxDNA export places inserted bases instead of refusing. Not
  yet dogfooded on prod (needs the deploy).
- **insertions/deletions, twist account** — shipped 2026-10-01 in the
  commit that added this line: `register.insertions`/`deletions` accepted;
  `chain_twist_global` (warn) per connected lattice-helix set when a
  helix's real-twist residual passes half a base; `chain_twist_register`
  skips a lone helix; sequence, pairing and exports honour them. The
  24-helix origami fixture now warns (≈137° per helix, no corrections),
  as it should. Dogfooded on prod 2026-10-01 (`dogfood-insdel-1`, 2 square
  helices × 64): +34.3°/helix warning, one deletion each → +0.0°, route nt
  drops; a single-stranded run split at the deleted offset (7 nt read as
  4 + 3, hiding `chain_floppy`) — fixed in the commit that added this
  sentence.

- **per-offset unpair** — shipped 2026-10-01 in the commit that added
  this line: `unpair(at='<helix>@<offset>', clear?)` writes
  `overrides[offset]='unpaired'` on both occupants; findings
  `chain_unpaired` (info), `chain_unpaired_run` (warn, folding question),
  `chain_unpaired_stray` (warn). A `set_domain` narrowing a domain past
  its own mark stays refused (clear the mark first). Dogfooded on prod
  2026-10-01 (`dogfood-hairpin-4`, stem@1 then stem@1–2, both cleared
  after): findings and refusals as specified; view='chain' still footed
  "every occupied offset is paired" and the pick label wrote `G·C` for an
  unpaired offset — both fixed in the commit that added this sentence.

- **`view='pick'` prod dogfood** — 2026-10-01 on `dogfood-hairpin-4`
  (stem atom by ordinal and by label, loop atom, every token read back:
  as specified) and `dogfood-nucleic-3`, realized before residue rows were
  stored: the pick dropped residue/pair/domain silently; fixed in the
  commit that added this line (the residue row names the residue and says
  to re-realize). Confirmed on prod after deploy; the same round found
  the unknown-uid and unbound-block refusals left the agent to find a uid
  — they now name `view='block'` and the block's own token.
- **td458169** — done 2026-09-30; results in the todo's text.
- **gr457928** — loop nucleotides chain by default: `realize_chain`
  relaxes the loop backbone unless `relax_loops=false` (Reto ruled
  default-on 2026-09-30 night); envelope_fit skips loop atoms and names
  a protruding atom by residue; residue rows persist. Tagged done.
  Prod-verified 2026-10-01 on `dogfood-hairpin-4` (the hairpin Reto saw
  with overly long bonds, rebuilt with the relax): worst loop O3'–P step
  9.74 → 1.96 Å, no envelope_fit finding, 4 complementary pairs.
- **base-pair complementarity is a read, not a look** — Reto on
  dogfood-nucleic-3 (2026-09-30): "I am not sure if basepairs in fact
  match"; the product could not answer (`pairing.watson_crick` had no
  caller, drc judged only *declared* families, view='chain' counted paired
  offsets without reading letters). Fixed in the commit that added this
  line: `chain_pairing_mismatch` (error) on an undeclared pair whose
  letters aren't Watson–Crick complements, and view='chain' tallies every
  pair's letters (complementary / MISMATCHED / unverifiable). Verified on
  prod's dogfood-nucleic-3 by hand first (all 21 pairs, 2.9 Å N1–N3).
  Reto ruled strict: an undeclared G·T is an error, a wobble is a declared
  `W-W-cis`.
- **gr458472** — filed 2026-09-30 from that re-dogfood: a `put(kind='se',
  ops=[…])` call lost its ops (put's schema has no `ops=`) and wiped the
  design; recovered by replaying design_revisions by hand. The refusal half
  is fixed in the commit that added this line (put with no ops refuses
  while the design has blocks). The rest — a restore_revision op, a
  revisions view, `ops=` on put's schema — is se persist work, not this
  thread's; it stays on the gripe.
- **backlog/se-fold-layout-coaxial-and-tails.md** — shipped 2026-09-30 and
  deleted, delete-on-ship: a helix reached through zero unpaired
  nucleotides is placed end to end on the one it stacks on (phase0 tuned so
  the backbone exits meet, one extra rise per bulged nucleotide), and an
  unpaired tail is a single-occupancy stub helix `<strand>.t5`/`.t3` — the
  representation `build_domain`'s own refusal of `loop_before_nt` on a
  first domain already named. Remaining refusals: no pairs, pseudoknot,
  > 10 000 nt, an already-routed strand.
- **backlog/se-walker-light-protocol.md** — landed in three slices
  (6e3fb3b1 and the B+C ship of 2026-09-30) and deleted, delete-on-ship;
  its decisions log is restated in `src/precis_se/chain/__init__.py`
  "Walker states" and the `precis-se-walker-help` skill.

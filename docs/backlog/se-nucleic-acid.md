---
status: ready
title: se nucleic acids — helix/strand/domain model, loops, relax, register + clash DRC, ViennaRNA fold checks
prio: high
model: opus
---

# `se` nucleic acids — DNA/RNA design in the block tree

## Motivation / why

The repo has zero nucleic-acid modelling (grep `nucleic|origami|Hoogsteen`
over `src/` → nothing; `protein` in `src/precis_bio/` is sequence→AlphaFold
only). The light-driven-walker research line and any origami/aptamer work
need DNA/RNA at four altitudes — tube with bend/twist limits → helix/loop/
strand layout → sequence, fold, pairing geometry → atoms per region — and
they must be usable **without** a walker. `se` already has the ladder: six
levels (`src/precis_se/__init__.py`), atomic mode realizing a block as
chemistry and binding a `structure` (`src/precis_se/atomic/generate.py`,
`atomic/bind.py`), per-block L2 jsonb columns (`dof`, `chromophore`), pair
relations in `se_topology` (`threading`), and the `formfind` precedent for
solver poses written back as `origin='proposed'`. This binds the
`precis_chain` kernel to that ladder.

Decomposition (scadnano's, chosen 2026-09-27): **helix carries geometry**
(path, motif, per-bp frames, tube); **strand routes** (sequence + ordered
domains `(helix, forward, start, end)`); **loop** = the ssDNA gap between two
domains, pinned at two backbone exit points, nucleotide count free; pairing is
**derived** from co-occupancy of a helix offset, never declared. Crossover =
0/1-nt loop between adjacent helices; foothold/toehold = single-occupied
domain; ssDNA scaffold region = single occupancy; hairpin = one strand, two
antiparallel domains on one helix + a loop.

## In scope

**Storage** — migration `src/precis_se/migrations/0015_se_chain.sql`:
`se_blocks.chain jsonb` (helix: `{role:'helix', motif, path:{waypoints_m |
lattice:{kind,row,col}}, n_units, phase0, register:{lattice, insertions,
deletions}, min_bend_radius_m?, min_gap_m?}`; strand: `{role:'strand',
sequence, nucleic:'DNA'|'RNA'}`); `se_topology.kind` CHECK →
`('threading','domain')` + a partial unique index `WHERE kind='domain'` on
`(ref_id, subject_name, (meta->>'ord'))`; domain row `subject_name`=strand,
`object_name`=helix, `meta={ord, forward, start, end, geometry?, overrides?,
loop_before_nt?}`. `SeBlock.chain`, `SeTree.domains`; `persist.py` gains the
column, a `_DOMAIN_COLS` (with `meta` — `_THREADING_COLS` has none) and a
kind-filtered load alongside threading.

**Ops, pure** (`precis_se/ops.py::_OPS`; vetting `precis_se/chain/vocab.py`):
`declare_helix`, `declare_strand`, `add_domain`, `set_domain` (edits one
row in place), `remove_domain`
(destructive by the `remove_` prefix rule), `clear_chain`, `layout_chain`
(materialises child segments `<helix>.s<k>`: `cyl:r<..>nmh<..>nm` envelopes
from `precis_chain.envelope.capsule_pose`, ports `5p`/`3p`; re-run retires
and regenerates, `origins['envelope']='proposed'`; default `max_seg_len` =
one lattice repeat, so a 7 kb design yields a few hundred children;
straight lattice helices may use `array_block`; `pattern-groups.md` would be
the collapse mechanism but is itself draft and unbuilt — nothing in this item
depends on it, `view='tree'` collapse is a follow-up). Lengths parsed at the boundary through the existing
units path; stored metres/radians.

**Ops, handler-level** (`precis_se/atomic/apply.py::HANDLER_LEVEL_OPS`,
human-Apply gated because they spend compute, read the store, or need an
optional dep): `relax_chain` (segment rigid bodies + hinges + loops +
crossover pins → `precis_chain.relax.relax_bundle`, Lp from `material` rows
via the `compose.py::LP_KEY` path; poses back as `origin='proposed'`,
user-origin poses move only under `move=`; one revision per call),
`fold_layout` (ViennaRNA dot-bracket → helix/strand/domain ops; lazy import,
`Unsupported` when absent; scaffold-length input allowed here only). Neither
runs in `design_turn`'s pure dry-run, so no double execution.
`realize_chain` joins this list in `se-nucleic-realize-export`.

**Derived pairing** — `precis_se/chain/pairing.py::derive_pairing(tree,
state=None)` (the `state=` kwarg exists from day one and is a no-op when
`None`; the same kwarg sits on `relax_chain(…, state=None)` — both are
filled in by `se-walker-light-protocol`, which must not have to change the
signatures): per
helix per offset the occupying (strand, domain, forward); exactly two
antiparallel occupants → pair; one → single-stranded; two parallel or three
or more → `chain_occupancy` error (triplex is out of scope for this cut).
O(total domain length), not O(n²).

**Constants** — `precis_se/chain/nucleic.py`, sources in the docstring:
B-DNA 0.334 nm / 34.3° (10.5 bp/turn) / r 1.0 nm; A-RNA 0.28 nm / 32.7°
(11 bp/turn) / r 1.15 nm; ssDNA contour 0.63 nm/nt; honeycomb 7 bp,
21 bp/2 turns; square 8 bp, 32 bp/3 turns; inter-helix centre spacing
2.5 nm default for both lattices (scadnano's `helix spacing`; measured
2.4–2.6 nm, Douglas 2009 / Ke 2009 — cite in docstring); oxDNA unit
0.8518 nm; Leontis–Westhof 12 families = 6 unordered edge pairs of
`{W,H,S}` × `{cis,trans}`, encoded `"W-W-cis"`, `"W-H-trans"`, … with
aliases `WC`, `wobble`, `reverse-Hoogsteen`, `mismatch` and
`ALLOWED_PAIRS[geometry]`. Persistence lengths (dsDNA 50 nm, dsRNA 60 nm,
ssDNA 2 nm) are `material` rows with conditions; **pure seams use the coded
default only**, store-read values reach `relax_chain` and the handler-side
findings. Default dsDNA min bend radius Lp/5 = 10 nm at **warn**; an
authored `min_bend_radius` promotes to error.

**Findings, pure** — `precis_se/chain/drc.py::findings(tree)`, one call in
`precis_se/drc.py::drc`: `chain_bend` error · `chain_twist_register` warn ·
`chain_clash` warn (kernel capsule pass over all segments; tol = `min_gap`;
skips consecutive same-helix segments and pairs sharing a crossover) ·
`chain_loop_short` error (`|exit_a − exit_b| > (n+1)·c + tol`, `tol` =
`nucleic.backbone_frustration_m` over the two helices; a 0-nt
crossover therefore requires exits within one bond — this IS the register
check) · `chain_loop_slack` info · `chain_floppy` info (single-stranded span
> coded Lp default) · `chain_dangling_domain` error · `chain_occupancy` error
· `chain_pairing_geometry` error · `chain_malformed` error (a stored
`chain`/domain payload that cannot be read at all, the `malformed_joint`
precedent — kept as its own rule, not folded into
`chain_dangling_domain`, because a corrupt record is not a routing error).
**Findings, handler-side** — appended in `handler.py::_render_drc` after the
pure pass, like `se_precedent.findings(store, tree, ref_id)`: when a
`material` Lp row exists, the pure `chain_floppy` rows are dropped and
re-emitted against the row's value (one row per span, never two);
`chain_fold_disagree` warn and `chain_offtarget` warn from
`precis_se/chain/fold.py` — fold checks bounded to strands ≤ 200 nt
(`RNA.fold` is O(n³)); off-target via a 6-mer hash index over all strands,
O(total length); `chain_fold_unavailable` info when `RNA` is absent.
**Exclusion** — `precis_se/validate.py::envelope_overlaps` skips every
segment↔segment pair (chain_clash owns them); the SDF budget goes to
chain-vs-non-chain pairs.

**Views** — `view='chain'` (helices: motif, n, turns, segments, occupancy;
strands: length, domains, loops; pairing table), registered in
`handler.py::_VIEW_ARGS`; `view='topology'` gains domain rows.

**Handoff to `se-nucleic-realize-export`** — atoms per region
(`realize_chain` + pure `chain/atoms.py::build_region`, no `GENERATORS`
entry, `to_pdb`) and scadnano/
caDNAno/oxDNA/PDB export live there. This item must leave them the seams
they need and nothing more: `derive_pairing(tree)` answers occupancy per
helix offset; `layout_chain` children `<helix>.s<k>` carry their segment
range in `chain` meta so a realizer can find the segment covering
`[start, end]`; `relax_chain` stores each placed loop curve (sampled
points, metres) on the strand's domain row `meta.loop_curve` so a later
realizer can tell a placed loop from an unplaced one.

**Skill + docs** — `src/precis/data/skills/precis-se-chain-help.md` (ops,
views, findings of this item; the realize/export rows are appended by the
follow-up item); op rows in `precis-se-help.md`; `precis-overview.md` se row
appends the skill; glossary entries (done).

## Explicitly NOT in scope

- Walkers, stations, occupancy states, light transitions, spectral budget,
  make-tree protocol (→ `se-walker-light-protocol`).
- Atoms per region (`realize_chain`, `to_pdb`,
  `structure` `view='pdb'`) and scadnano/caDNAno/oxDNA/PDB export
  (→ `se-nucleic-realize-export`, split 2026-09-28).
- Protein import (→ `se-protein-chain-import`).
- Triplexes and parallel duplexes: reported as `chain_occupancy`, not
  modelled (needs a third backbone azimuth and a geometry on the third
  domain).
- A new binding kind. Tube geometry is derived from the block's own `chain`
  declaration; L3 stays a bound `structure`. Reopen only if oxDNA-relaxed
  conformers must be stored.
- Running oxDNA/CanDo; whole-origami atoms (≈470k atoms for 7 kb) — atoms per
  region only.
- Thermodynamics, melting, salt dependence, knot/threading detection —
  `relax_chain` is a mechanical settle; stated in the skill.
- Fold checks on strands > 200 nt outside `fold_layout`.
- Constraint-composed paths (solver choosing unit counts); insertion/deletion
  global twist check (register hook reserved); backbone-ribbon rendering in
  `precis.viz3d`.

## Acceptance criteria

- Hairpin `GGGGAAAACCCC`: `fold_layout` → 1 helix (4 bp), 1 strand, 2
  domains, 4-nt loop; `derive_pairing` → 4 `W-W-cis`; before `relax_chain`
  the loop's domain row has no `meta.loop_curve`, after it a sampled curve
  whose ends sit within one bond of the two `backbone_exit` points.
- Four-helix lattice ribbon (4 helices, 4 strands, 8 domains, 4 zero-nt
  crossovers at register-correct offsets) authored in one `put`; `view='drc'`
  has no `chain_*` error; shifting one crossover by 1 bp fires
  `chain_loop_short`. A *radiating* four-arm (Holliday) junction is a
  follow-up fixture, not this criterion — see the log.
- Rectangle origami built procedurally in the test (24 helices × 256 bp
  square lattice, scaffold + ~100 staples, ≈192 segments): `layout_chain`
  < 2 s; `relax_chain` < 30 s; `view='drc'` < 5 s with zero
  `overlap_budget_exceeded` and zero calls to
  `precis_se.validate.cad_relate.clearance` (monkeypatched) where both names
  match `<helix>.s<k>`; every segment child's `chain` meta names its
  `[start, end]` range and the ranges tile each helix exactly.
- 21 bp honeycomb range passes, 22 bp fails `chain_twist_register`; parallel
  helices at 2.0 nm centre spacing → `chain_clash`, at 2.5 nm → clean.
- Loop of 3 nt across 3 nm → `chain_loop_short`; 20 nt across 3 nm →
  `chain_loop_slack`.
- Relax: two helices joined by a 2-nt loop from 6 nm settle to exit-to-exit
  ≤ 1.9 nm; poses read back `origin='proposed'`; a `user` pose is unchanged
  without `move=`; `relax_chain` appears in `HANDLER_LEVEL_OPS` and is a
  proposal in the web turn.
- `RNA` unimportable → `view='drc'` still renders with one
  `chain_fold_unavailable`; installed → hairpin MFE `((((....))))` and a
  planted 8-nt staple–staple complement → `chain_offtarget`; a 6 kb scaffold
  is skipped by the fold check with its length named.
- Three strands on one offset → `chain_occupancy`.
- Declared `geometry:'W-H-trans'` on a domain whose letters cannot pair
  that way → `chain_pairing_geometry`.
- All new lengths refuse bare numbers (`UnitRequiredError`) and store metres.

## Target + blast radius

`se` handler (`src/precis_se/handler.py` views drc/topology/chain,
`_VIEW_ARGS`), `precis_se/ops.py`, `persist.py`, `validate.py`, `drc.py`,
`atomic/apply.py::HANDLER_LEVEL_OPS` (`relax_chain`, `fold_layout`), new
`precis_se/chain/`, se-plugin migration 0015,
`pyproject.toml` (`[chain]` extra = ViennaRNA), skills `precis-se-chain-help`
/ `precis-se-help` / `precis-overview`. No worker, no web route. Deploy: the
`[chain]` extra must be added to the MCP serve role and the UV_WITH bridge
(`docs/backlog/mcps-venv-deploy-gaps.md` class) or fold checks silently
report `fold_unavailable` in prod.

## Open questions / decisions log

- 2026-09-27 helix-carries-geometry over strand-carries-geometry: a scaffold
  snakes across every helix, so a "scaffold tube" is meaningless; scadnano
  export becomes near-identity. Decided.
- 2026-09-27 relax A + B in the first cut: bend/clash on authored (straight)
  origami geometry is vacuous; settled geometry is what the checks must see.
  Decided.
- 2026-09-27 ViennaRNA as `[chain]` extra + lazy import + `Unsupported`;
  promote to core when the wheel resolves on every CI/deploy platform.
  Decided, revisit at ship.
- 2026-09-27 rectangle fixture procedural in the test. Decided.
- 2026-09-27 (review) loop contour (n+1)·c and the register check IS
  `chain_loop_short` at n=0; `relax_chain`/`fold_layout`/`realize_chain` are
  handler-level; region realization needs `realize_chain` because generators
  are tree-blind; triplex out; segment↔segment pairs excluded from the SDF
  scan wholesale (connects do not save the broad phase). Decided.
- 2026-09-28 per-position geometry is `overrides` on the domain row, not a
  1-bp domain (keeps domain count = scadnano domain count; already the
  `meta` shape in Storage). Decided.
- 2026-09-28 inter-helix spacing is one constant (2.5 nm) with a per-design
  `min_gap` override (already in Storage/Constants). Decided.

- 2026-09-28 (ready gate): blocker — `GENERATORS["dna"]` is defined as
  "sequence in, straight duplex out, no tree" but the hairpin acceptance
  criterion has it "realize" a folded structure with a 4-nt loop (not a
  straight duplex); the spec never says whether the generator emits atoms
  for the stem only or for stem+loop, or how a tree-blind generator gets
  loop/bend geometry (`precis_chain.loop.loop_curve` needs pinned exits a
  standalone sequence-in call doesn't have). Two different builds are
  reasonable from this text.
- 2026-09-28 (ready gate): advisory — both `OPEN:` entries above are
  already answered by the body text (`overrides?` is already in the domain
  `meta` shape, line 44; 2.5 nm default + per-design `min_gap_m?` override
  are already in Storage/Constants) — they read as unresolved only because
  the log line was never updated to "Decided". Not a build ambiguity, but
  the template's own rule (no blocker-severity OPEN at `status: ready`)
  reads these as still-open; tidy the wording before flipping status.
- 2026-09-28 (ready gate): advisory — the new `chain_*` dotted finding
  names (`chain_bend`, `chain_clash`, `chain_loop_short`, etc.) break the
  codebase-wide flat snake_case rule-naming convention (verified: every
  existing `rule=` in `precis_se/drc.py`, `atomic/validate.py`,
  `validate.py` — `bond_length_sanity`, `overlap_budget_exceeded`,
  `mode_binding_mismatch`, 30+ names, zero dots). Not DB-enforced, so not a
  build blocker, but an unacknowledged convention deviation worth a
  decision line.
- 2026-09-28 (ready gate): advisory — "straight lattice helices may use
  `array_block` and `pattern-groups.md` is the collapse mechanism" cites
  `docs/backlog/pattern-groups.md`, which is itself `status: draft` and
  `blocked-by: design-state-core` (unbuilt, blocked on another unbuilt
  item), phrased as already available. No acceptance criterion depends on
  it (the rectangle-origami AC expects ~192 discrete segments, not a
  collapsed representation), so it doesn't block a build — but the sentence
  should say "future" or drop the reference, and this dependency isn't
  declared via `blocked-by`.
- 2026-09-28 (ready gate): advisory — "Target + blast radius" names
  `precis/structure/export.py` + "structure handler view" but the actual
  view dispatch (`_EXPORT_VIEWS`, the `view == "poscar"`/etc. branches) is
  in `src/precis/handlers/structure.py`, not under `precis/structure/`.
  Name the exact handler file so the post-deploy blast-radius check looks
  in the right place.
- 2026-09-28 (post-gate) resolved in the body: `GENERATORS["dna"]` is the
  tree-aware realizer behind `realize_chain` (helix/region in, units from
  `fibre.unit_frames`, `Unsupported` for an unrelaxed loop — hairpin AC
  reworded to the stem); DRC rule names are flat snake_case `chain_*` (house
  convention, dotted spelling was a draft artefact, also applied to
  `se-walker-light-protocol`); `pattern-groups.md` reference marked
  unbuilt/non-dependency; Target names `src/precis/handlers/structure.py`;
  `derive_pairing(tree, state=None)` / `relax_chain(…, state=None)` carry
  the walker's kwarg from day one as a no-op. Decided.
- 2026-09-28 (ready gate) split signal, not a blocker: In-scope bundles
  three separable slices — (1) data model, pure ops, `derive_pairing`,
  constants, pure `chain_*` DRC, chain/topology views; (2) handler-level
  `relax_chain` + `fold_layout` behind the `[chain]` extra; (3) atomic
  realization + interop export. (1) is prerequisite to both; (2) and (3) do
  not need each other; one opus agent landing all three unattended is a
  human's call.
- 2026-09-28 Reto: split. (3) moved to `se-nucleic-realize-export`
  (blocked-by this item); this item keeps (1)+(2) and now owes the realizer
  two seams (segment range in child `chain` meta, placed loop curve on the
  domain row). `se-walker-light-protocol` re-pointed to block on the new
  item. Decided.
- 2026-09-28 the child's ready gate retired "tree-aware `GENERATORS["dna"]`"
  (post-gate line above): `Generator` is `builder(params)` with no tree, so
  the realizer is `realize_chain`'s handler code + a pure `build_region`
  helper, no registry entry. Recorded here so the line above is read as
  superseded. Decided.

### Slice 1 built 2026-09-28 — decisions taken during the build

Storage (`0015_se_chain.sql`), the six pure ops of slice 1, `derive_pairing`,
`chain/nucleic.py`, the pure `chain_*` DRC pass, the segment↔segment
exclusion and `view='chain'`/`view='topology'` are **built**. Slice 2
(handler-level `relax_chain`/`fold_layout`, the `[chain]` extra, the
handler-side findings, `meta.loop_curve`, the skills) is untouched. Each
line below is a place the build departs from the text above, with the
reason — read the code as authoritative where they disagree.

- **B-DNA twist is `2π/10.5` exactly, not the 34.3° this spec quotes.**
  At 34.3° a 21-bp honeycomb repeat misses two whole turns by 5.2e-3 rad,
  five times `precis_chain.register.REGISTER_TOL`, so *every* honeycomb
  design would report `chain_twist_register` — including the acceptance
  criterion that says 21 bp passes. The rounded degree figure IS 2π/10.5;
  only its precision was load-bearing.
- **A declared lattice retunes the motif's twist**
  (`nucleic.lattice_motif`): honeycomb 21 bp/2 turns is B-DNA's own
  10.5 bp/turn (a no-op), square 32 bp/3 turns is 10.67 bp/turn. The spec
  lists both numbers for one motif, which is inconsistent; the lattice's
  repeat wins for register, because that is what the crossovers hold the
  helix at. 256 bp on the square lattice is in register only under this
  rule.
- **The default clash tolerance is measured from 2.4 nm, the LOW end of
  the measured 2.4–2.6 nm spacing, not the nominal 2.5 nm**
  (`nucleic.HELIX_SPACING_MIN_M`). A tolerance set exactly at nominal
  spacing is a knife-edge float comparison — the rectangle fixture
  reported 37 spurious `chain_clash` warnings (measured) before this
  constant existed, because a site at `row * 2.5e-9` lands a few ULP
  inside its own gap. Both acceptance numbers still hold: 2.0 nm fires,
  2.5 nm is clean, now with 25 % of margin.
- **`layout_chain` splits by unit count, not
  `precis_chain.envelope.capsules_along`.** `capsule_pose` IS used, as the
  spec says; the *splitter* is not, because its arc-length-uniform cuts
  land between units and the realizer seam this item owes requires the
  child ranges to tile the helix's unit range exactly.
- **A segment's `pose` is stamped `origin='proposed'` too**, not only its
  `envelope`. An unstamped pose reads as `user`, which is contract, and
  `relax_chain` would then refuse to move the very children it exists to
  settle.
- **Segment envelopes are bare metres** (`cyl:r1e-09h1.0354e-08`), not the
  spec's `cyl:r<..>nmh<..>nm`: an se envelope is canonical/storage mode
  (`precis.cad.dsl.parse`'s `require_units=False`), the same rule the
  atomic mode's hand-authored `sphere:r2e-10` follows.
- **A strand's `sequence` is optional.** The spec's shape lists it
  unconditionally; a 24-helix rectangle is a real design long before its
  7 kb exists, so every letter-dependent check reports *unverifiable*
  instead (`N` likewise).
- **A tenth rule, `chain_malformed` (error)**, beyond the nine listed: the
  defence-in-depth every other stored jsonb payload in se gets
  (`malformed_joint`). Without it a hand-corrupted record crashes the read
  path instead of surfacing there.
- **`register.insertions`/`deletions` are refused when non-empty** rather
  than stored and ignored — the kernel's own reserved hook
  (`precis_chain.register.phase_after`'s `per_unit_twist`) raises for the
  same reason: a global twist correction nothing applies would read as
  checked.
- **A segment's `5p`/`3p` ports are skipped by `unconnected_port`.** They
  are anchors on a derived child (the backbone's continuation IS the unit
  tiling), and 192 segments × 2 ports would add 384 info rows to
  `view='validate'`.
- **Strand azimuths are antipodal (0, π)**, not B-DNA's real ~120°/240°
  minor-groove pair (`nucleic.STRAND_AZIMUTH_RAD` states the consequence:
  it moves a reach number by less than the bond it is compared against,
  and the groove angle is an atoms-tier number).
- **`ALLOWED_PAIRS`'s contents are a curated occupancy table**, not an
  exhaustive one — the spec named the key, not the rows. Sourced from
  Leontis–Westhof 2001 + the 2002 isostericity matrices, and
  `chain_pairing_geometry`'s text says "per this table" rather than
  claiming completeness.
- **`_DOMAIN_COLS` carries the uid columns as well as `meta`** — the
  uid-as-join / name-as-display rule 0009 gave every other se
  cross-reference, which `_THREADING_COLS` already follows.
- **The rectangle criterion's "zero `clearance` calls" is asserted over
  `view='drc'` AND `view='validate'`.** The exclusion lives on
  `validate.envelope_overlaps`, which `view='drc'` does not call, so
  naming only `drc` would have made the assertion vacuous.
- **The 4-way-junction fixture is a four-helix square-lattice ribbon with
  four crossovers, not four radiating arms.** With one twist and one
  `phase0` per helix, the classic four-arm ring's four crossovers cannot
  all be register-correct: `3·(k − k₀) ≡ 16 (mod 32)` has no solution that
  also satisfies the +y pair. The criterion's counts (4 helices, 4 strands,
  8 domains, 4 zero-nt crossovers at register-correct offsets) all hold.
- **The honeycomb site mapping is derived here, not transcribed**
  (`nucleic.site_position`): `x = col·s·√3/2`, `y = s·(1.5·row +
  0.5·((row+col) mod 2))`, chosen so every nearest neighbour is exactly
  one spacing away and each site has three — a property the tests check
  rather than a formula taken on trust.

### Slice 1 dogfood, 2026-09-29 (naive agent, MCP surface, throwaway DB)

A naive opus agent with only the `precis` tool and no repo access built a
hairpin and a 4-helix square-lattice tile, then broke each deliberately
(loop cut to 1 nt; one crossover moved 1 bp). Both breaks were caught with
exactly one new error on exactly the changed element. **Zero call-shape
failures across 31 calls** — every op was accepted first try from the skill
alone, including the nested `register`/`path`/`overrides` dicts, so the
slice-1 skill is doing its job. Derived pairing, the `(n+1)·c` contour
arithmetic and `chain_pairing_geometry` over a G·A mismatch all behaved.

- **Slice 2 additions from the dogfood** (each cheap, each a real mistake the
  tool let through or made awkward):
  - `chain_pairing_geometry`'s message prints the coded occupancy as an **RNA
    alphabet** (`A·U, C·G, G·C, G·U, U·A, U·G`) to a DNA designer. `T·A` does
    pass — the T/U folding works — but the text reads as if it would not.
  - `view='chain'`'s `segments` column before `layout_chain` shows the default
    `max_seg_len` (a 4-unit helix read `1 × 21 units`), not the tiling it will
    get. Show the prospective tiling or say "not laid out".
- **Two blockers for an orderable design, now their own items:**
  `docs/backlog/se-chain-insertions-deletions.md` (the refused
  `register.insertions`/`deletions` hook — real sheets need the twist
  correction) and `docs/backlog/se-chain-staple-sequences.md` (no
  complementarity fill, so the strands you would paste into an order form
  cannot be produced; carries the missing `chain_sequence_length` check,
  which currently lets a 17-nt sequence sit on a 9-nt route silently).
- Undocumented but working, worth folding into the skill: `text=` accepts a
  native dict, not only a JSON string, and `layout_chain` rides inside a
  `put` ops list as well as `edit`.

### Slice 1 follow-ups found while writing the skill (2026-09-29)

- **The slice-1 skill is written and landed** (`precis-se-chain-help.md`, plus
  a pointer section in `precis-se-help.md` and the `precis-overview.md` se
  row). Slice 2 still owes the `relax_chain`/`fold_layout` rows, but "the
  skills" are no longer wholly slice-2 work — slice 1's six pure ops, both views and
  the ten findings are documented against the code as built.
- **A missing finding, newly named:** two domains occupying one offset may
  declare *different* Leontis–Westhof families, and nothing reports it — the
  first declaring occupant wins silently. `chain_pairing_geometry` only
  catches letters that cannot pair the declared way, not two domains
  disagreeing with each other. `pairing.py::OffsetOccupancy`'s comment
  claimed the DRC pass reported it; that comment was aspirational and is now
  corrected to state the silent behaviour. Slice 2 adds the rule (suggested
  `chain_pairing_disagree`, error, pure — both declarations are in the tree).

### Slice 2 pass A built 2026-09-29 — the strand azimuth, and the register table

`STRAND_AZIMUTH_RAD`'s antipodal `(0, π)` is **gone**. What replaced it, and
the numbers that follow from it:

- **δ = 144° for B-DNA, 220.5° for A-RNA** — the azimuthal separation of
  the two backbones across the **minor-groove** side
  (`nucleic.MINOR_GROOVE_SPAN_RAD`), with the two strands symmetric about the
  frame normal (`−δ/2` forward, `+δ/2` reverse), so the normal is the base
  pair's pseudo-dyad and bisects the minor groove. A-RNA's exceeds 180°
  because the A form's minor groove is the **wide** one.
- **B-DNA's 144° is stated outright by two independent primary sources**, not
  derived: Kornyshev & Leikin, *Phys. Rev. E* 62:2576 (2000), "phi_s (≈0.4 π)
  is the azimuthal half-width of the minor groove" (⇒ 0.8 π); and Allahyarov,
  Löwen & Gompper, *Phys. Rev. E* 68:061903 (2003), whose two-strand
  parametrisation sets one phosphate strand's `phi_0` to 0° and the other's to
  **144°**. Bohr & Olsen (arXiv:1102.0761) state the 144°/216° pair directly.
- **Cross-checked by an independent route and independent data.** The groove
  widths (B-DNA 5.7/11.7 Å, El Hassan & Calladine's "shortest inter-strand
  P···P less 5.8 Å" convention) give `δ = 2π·P_minor/(P_minor + P_major)` =
  **142.8°** — 0.8 % from the cited 144°. And running it the other way:
  place phosphates at the sourced `P_RADIUS_M` = 8.9 Å (Allahyarov 2003,
  "centered at a radial coordinate of 8.9 Å") and the model's own two channels
  come out **11.8 Å and 17.5 Å** against the tabulated 11.5 and 17.5 (+2.7 %,
  +0.0 %). Both checks are tests, not prose.
- **A-RNA's 220.5° is the inference, not a citation** — no source states an
  A-form backbone separation. It is the width-ratio rule over A-RNA's *own*
  widths, minor 10.8 Å / major 4.7 Å (Šulc et al., *J. Chem. Phys.*
  140:235102, 2014, in a Curves+-analogous convention), read as **220° ± 10°**.
  Using A-**DNA**'s 11.0/2.7 Å instead would have given 239° — a 19° error, so
  the RNA-specific numbers mattered.
- **The "12 Å / 22 Å" figures imply δ = 127° instead, 17° below the cited
  value.** They cannot be P···P distances in this convention: 22 Å exceeds the
  17.8 Å diameter of the phosphate cylinder itself. Their own convention is
  unstated at source (traceable only to Wikipedia citing Wing et al., *Nature*
  287:755, 1980, unverified there); 12 + 22 = 34 Å being one pitch makes an
  axial-span reading likely but that is inference.
  `se-nucleic-realize-export`'s criterion needs to name its convention.
- **`phase0` is re-referenced.** It used to be the forward backbone's own
  azimuth; it is now the minor-groove bisector's. A design carries over
  unchanged under **`phase0 += π/2`**, which preserves every register-correct
  offset in *both* crossover directions — that is how both test fixtures
  migrated.
- **A new threshold: the spec's `tol`.** `chain_loop_short` compares against
  `(n+1)·c + tol` with `tol = Σ r(1 − sin(δ/2))` over the two helices
  (`nucleic.backbone_frustration_m`; 49 pm each for B-DNA, 98 pm for a
  B-DNA pair, **zero** within one helix and zero for antipodal backbones).
  A duplex's two backbones cannot both face a neighbour at once, and the
  shortfall is a fact about B-DNA, not about the routing — the same argument
  as `HELIX_SPACING_MIN_M`. **The literature says so in as many words**:
  Rothemund's origami supplement (*Nature* 440:297, 2006) — "at crossover
  points, strand backbone positions should fall at the tangent point between
  helices", but "because of the non-integral number of bases in a single turn,
  and the major-minor groove angle, it is not possible to put all crossovers
  in this optimal orientation", so designs "invariably incorporate features
  that should cause strain". Without the allowance a register-correct
  crossover sits 32 pm inside a 630 pm threshold, the azimuthal window
  collapses from ±9.8° (antipodal) to ±5.2°, and the honeycomb's second
  crossover family — the ±5 bp one, 8.57° off — stops being reachable at all.
  With it the window is ±10.9° and both families reach.
- **Measured reaches moved as expected**: a register-correct 0-nt crossover's
  exit gap is **0.598 nm**, not slice 1's 0.500 nm (= 2.5 − 2 r sin(δ/2), not
  2.5 − 2 r); a 1 bp shift gives 0.890 nm, not 0.932 nm.

**The per-neighbour register table** (B-DNA, every helix at the same
`phase0 = 0`, offsets mod the lattice repeat, computed from the exits and
cross-checked against the closed form). This replaces the dogfood's
confounded "4× stricter than caDNAno" claim, which counted the *aggregate*
crossover period against a *per-neighbour* one:

| lattice | neighbour | fwd→rev | gap | rev→fwd | gap |
|---|---|---|---|---|---|
| honeycomb (repeat 21) | +30° (`col+1`) | **14** | 0.598 nm | 9, 19 | 0.681 nm |
| | +150° (`col−1`) | **7** | 0.598 nm | 2, 12 | 0.681 nm |
| | −90° (`row−1`) | **0** | 0.598 nm | 5, 16 | 0.681 nm |
| square (repeat 32) | 0° (`col+1`) | 24 | 0.598 nm | 8 | 0.598 nm |
| | +90° (`row+1`) | 16 | 0.598 nm | 0 | 0.598 nm |
| | 180° (`col−1`) | 8 | 0.598 nm | 24 | 0.598 nm |
| | −90° (`row−1`) | 0 | 0.598 nm | 16 | 0.598 nm |

- **Per neighbour, one offset per lattice repeat — 21 bp honeycomb, 32 bp
  square.** Aggregated over the 3 (resp. 4) neighbours that is one crossover
  site every 7 (resp. 8) bp, which is `LatticeSpec.crossover_period` exactly.
  The model was never 4× stricter than caDNAno; 8 bp *is* 32 bp divided by
  four neighbours.
- **The fix validates itself on the honeycomb.** Corrected, one crossover
  direction's offsets are **0, 7, 14 — caDNAno's own honeycomb crossover
  positions, at zero azimuthal error**. Antipodal put them at 1, 8, 15, each
  4.29° off register (gap 0.527 nm against a 0.500 nm floor). The square
  lattice does not discriminate: both models give {0, 8, 16, 24}, only
  permuted across the neighbours, because 90° is a whole multiple of the
  square repeat's 11.25° azimuth grid and 30°/150° are not multiples of the
  honeycomb's 17.14°.
- **New, and the antipodal model hid it: the two crossover directions are no
  longer one condition.** `phase0 + k·twist ≡ azimuth ± π/2`, `+` for
  forward→reverse and `−` for reverse→forward
  (`nucleic.crossover_phase_rad`), so the two families are half a turn apart.
  On the honeycomb (an odd 2 turns per 21 bp) only one family lands on integer
  offsets; the other gets two strained offsets per turn at 0.681 nm, **±5 bp
  from the exact ones** — which is exactly the documented relationship: Douglas
  et al., *NAR* 37:5001 (2009), "scaffold crossovers … five base pairs, or half
  a turn, upstream or downstream of allowed crossover positions for the
  associated staple helices", and Rothemund 2006's "a crossover involving
  staple strands is in tension with an adjacent crossover involving the
  scaffold strand". The model reproduced the 5 bp and the tension without
  being told either.
  **Do not read the two directions as "scaffold" and "staple"**: a strand
  crossing `h0→h1` forward→reverse crosses `h1→h2` reverse→forward, so both
  strand types use both families. The sign of δ swaps which family is the
  exact one and nothing in this tier pins it; what is pinned is the 5 bp
  separation.
- **One unchased caveat, recorded so nobody re-derives it.** Ke et al. 2009
  give the square lattice's neighbour walk as "north 0 bp → west 8 → south 16
  → east 24", i.e. the crossover site advances **+90° per 8 bp**; the model
  advances −90° (offsets 0/8/16/24 serve south/west/north/east). Magnitudes
  all agree — 90° per 8 bp, four neighbours, 32 bp per neighbour — and a
  reflection maps both lattices onto themselves (the honeycomb's 30°/150°/270°
  set is symmetric about the y axis), so this is a viewing/handedness
  convention in `site_position`, not an observable. Chase it only if an
  exporter has to agree with caDNAno column-for-column.
- **The kernel needed no change** beyond `register.crossover_positions`'s
  docstring, which said "backbone" where it means the unit's own azimuth.
  `fibre.backbone_exit` already took the azimuth as a parameter, and no
  chemistry entered `src/precis_chain/`.

Also in this pass: the pure **`set_domain`** op (`strand` + `ord` select the
row; any `add_domain` field except `ord` changes it, absent meaning
unchanged, explicit `null` clearing) — the dogfood's 11-ops-for-1-bp
complaint, now one op that runs in `design_turn`'s dry run; and the register
rule documented in `precis-se-chain-help.md`, with `chain_loop_short` now
naming the landing offsets that would reach.

### Slice 1 rulings on the build's three open questions (2026-09-28)

- **The four-helix ribbon fixture is accepted for this criterion, but the
  impossibility argument behind it is NOT established.** The build reported
  that a radiating four-arm junction "cannot" have four register-correct
  crossovers because `3·(k − k₀) ≡ 16 (mod 32)` has no integer solution. It
  does: 3 is invertible mod 32 (3·11 ≡ 1), so `k − k₀ ≡ 16 (mod 32)`. The
  joint system over both crossover pairs was not written out or checked, so
  treat "a four-arm ring cannot be register-correct" as an untested
  conjecture, not a proven constraint — anyone building the radiating
  fixture should solve the real system rather than trusting this line. What
  the accepted fixture does test (register-correct zero-nt crossovers, and a
  1 bp shift firing `chain_loop_short`) is the same in either shape.
  Decided.
- **`chain_malformed` stays a tenth rule**, now in the body's list above.
  Decided.

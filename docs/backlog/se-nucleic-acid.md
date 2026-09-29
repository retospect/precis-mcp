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

**Landed UNGATED and never suite-verified — ownership of the triage.** Pass A
went to `main` as **90a12c0f8** by `scripts/ship --quick` on 2026-09-29, at
Reto's explicit instruction during a fleet wind-down. No full gate ran on it,
and it is the third unverified layer on `main`: the last green verdict is
`f8f884d15` (23732 passed, 1h43m), after which `b81bf3cce` was deployed with
no verdict, this pass landed, and `8e037c015` (taxonomy discovery rewrite)
landed ungated too. What DID run here: the targeted chain suite 264 passed,
`scripts/test --impacted` 15427 passed / 0 failed / 295 deselected before
crashing inside testmon's own `pytest_runtest_logreport` hook (the known
`.testmondata` flake, which cleared the cache), `mypy src tests` clean, ruff
clean. That is not a gate.

So when the next full gate over `main` comes back red, these are the files to
look at first and they belong to this item, not to whoever happens to be
running the gate: `src/precis_se/chain/nucleic.py`, `chain/layout.py`,
`chain/drc.py`, `src/precis_se/ops.py`, and `tests/test_se_chain_drc.py`,
`test_se_chain_ops.py`, `test_se_chain_origami.py`. The likeliest failure is
not the azimuth itself but the `chain_loop_short` `tol` (the 98 pm backbone
frustration term): it was introduced *because* the corrected δ leaves a
register-correct crossover 32 pm inside a 630 pm threshold, so it is the one
number in this pass that exists to make the rest pass. It is zero for
antipodal backbones by construction and Rothemund's 2006 supplement states the
physics, but it deserves a second opinion before anything depends on it.

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

### Slice 2 pass B1 built 2026-09-29 — the three dogfood/skill findings

All three are **built and green** (`tests/test_se_chain_ops.py` +
`test_se_chain_drc.py`, 60 passed), uncommitted at the time of writing.

- **`chain_pairing_disagree` (error, pure)** — the rule the skill pass named.
  `pairing.OffsetOccupancy` gains `declarations`: every occupant that declared
  a family, canonicalised through `nucleic.canonical_geometry`, in the order
  `geometry`'s first-wins pick uses, so `declarations[0]` **is** the winner and
  nothing downstream changed behaviour. `WC` against `W-W-cis` is therefore not
  a disagreement. The message says which declaration every other check is
  using and that the other is being ignored — an offset cannot be two families,
  so the fix is to change one, per-position via `overrides[offset]` if the two
  domains need different families elsewhere.
- **`chain_pairing_geometry` now prints the helix's own alphabet.**
  `ALLOWED_PAIRS` stays RNA-keyed (`canonical_base` folds T onto U); only the
  *message* substitutes T for U on a non-RNA helix, so the wobble reads `G·T`.
  A helix whose geometry could not be realised defaults to DNA lettering —
  that design already has a `chain_malformed` row.
- **`view='chain'`'s `segments` cell was only half-fixed on the first pass, and
  the half that remained was the dogfood's actual complaint.** Gating on
  "laid out or not" still printed `{n} × {max_seg_len} units` afterwards, so
  the 4-unit helix that read `1 × 21 units` before `layout_chain` read
  `1 × 21 units` after it too. The cell now reports **the ranges' own unit
  counts** (`1 × 4 units`), and reads them off **the stored segment children**
  rather than a recomputed tiling — the column is a claim about the tree, and a
  `max_seg_len` changed since `layout_chain` ran would make the two disagree.
  A trailing short segment makes the spans non-uniform, so the cap prints as a
  bound (`≤`) rather than a figure. Both branches are asserted.

**The `[chain]` extra's promote-to-core condition is met, and it is still an
extra.** The 2026-09-27 decision was "promote to core when the wheel resolves
on every CI/deploy platform". Measured 2026-09-29: `viennarna 2.7.2` resolves
for manylinux x86_64, manylinux aarch64, win_amd64 and darwin, on both 3.12
and 3.13. It stays an extra anyway — putting a compiled RNAlib in every
serve/worker venv for a niche capability is a footprint decision, not a
platform one, and that is now the only question left. ViennaRNA is in the
`dev` group too (grimp's precedent), so CI exercises the fold checks'
*installed* branch and not only the `chain_fold_unavailable` degradation;
that needs a dev-image rebuild to activate in-container, and until then those
tests `importorskip("RNA")`. A promotion must also add `viennarna -> RNA` to
`scripts/lib/check-core-deps.py`'s `IMPORT_NAME_OVERRIDES` — the module the
distribution installs is `RNA`, which that script's default name guess misses.

### Slice 2 pass B2 built 2026-09-29 — `relax_chain`, `meta.loop_curve`, and the first superseding finding

`relax_chain` is live (`precis_se/chain/relax.py`, dispatched from
`atomic/apply.py::HANDLER_LEVEL_OPS`, now 6 names). What it settles, and the
numbers that came out:

- **Two helices 6 nm apart (axes), joined by a 2-nt loop, settle to
  exit-to-exit 1.875 nm** against the criterion's ≤ 1.9 nm, from 4.908 nm —
  the loop's own `(2+1)·0.63 = 1.89 nm` contour, so the settle stops where the
  backbone actually holds it rather than collapsing the exits. `converged=True`
  in 181 FIRE steps. Note the criterion's "6 nm" is the **axis** separation:
  the exits start 4.908 nm apart, not 6, because each sits 1 nm off its axis at
  a groove-asymmetric azimuth.
- **The 192-segment rectangle settles in 0.42 s** (criterion: < 30 s), 200
  steps, `converged=True`, 119 loop springs (23 scaffold turns + 96 staple
  crossovers) and 168 hinges, 192 proposed poses and 119 loop curves written.
- **The bundle is built in nanometres, not metres.** At metre-scale
  coordinates every force in a nucleic-acid design is ~1e-9, so
  `relax_bundle`'s default `tol=1e-6` is met at step 0 and its default
  per-step cap (5 % of a body length) is ~3e-10 — a settle that reports
  `converged` without moving, and cannot cross 4 nm in 500 steps. Convergence
  is checked at `TOL_NM = 1e-4` (a residual loop extension of 0.1 pm).
- **`meta.loop_curve`** (the `se-nucleic-realize-export` seam) is a new
  `DomainSpec` field on the LATER domain of a loop, 16 sampled points in
  **metres**, computed from the **settled** exits with the outward radial
  directions as the Hermite tangents. Derived, never authored: `add_domain`/
  `set_domain` do not accept it and a `set_domain` edit drops it (a route that
  moved no longer has the curve that was settled for it). `None` vs a list is
  the seam, so an empty list is never stored.
- **Write-back contract is `formfind`'s**, with one addition: a segment whose
  pose is user contract is **hard-pinned** at both beads rather than merely
  skipped, so the user's placement constrains the settle instead of being
  quietly contradicted by it. Bodies start from the children's *current*
  placement (a second `relax_chain` continues rather than resetting), and the
  settled axis is re-scaled to the body's own length before writing, so the
  stored `cyl` envelope and the pose cannot drift apart (the kernel's rigid
  term is a stiff spring, not a constraint).
- **Lp enters through the motif, not a second formula**:
  `dataclasses.replace(motif, persistence_length=<store row>)` into
  `relax.hinge_stiffness`, so the WLC constant stays the kernel's single copy.
  The row is read by `relax.material_lp_m` — one reader, shared with the
  finding below, so the settle and the check that grades it can never disagree
  about which row won. The row's unit comes from the **property registry**
  (`compose.unit_label`, `nm` by default), not from the value row itself;
  a unit that will not convert to a length is treated as **absent**.
  `tree.own_slug` is what the resolution keys on, and `persist.load_tree`
  deliberately does not set it — so a settle only sees `material` rows when it
  runs through the handler.

**The handler-side `chain_floppy` re-emission is the first pass in `se` that
replaces a pure finding.** `precis_se/chain/findings.py::findings` returns a
`HandlerFindings(supersedes, rows)` and `handler.py::_render_drc` applies the
drop — the destructive step lives at the one call site that owns the findings
list, never inside the pass. The rows themselves come from the pure
`chain_drc.floppy_findings(..., lp_of=)` hook, so both tiers measure a span
through the same code; the re-emission rebuilds **every** helix's rows (the
ones with no row keep the coded default and say so), which is what makes "one
row per span, never two" true rather than hoped for. With no `material` row
anywhere the pass returns nothing and the pure rows stand byte-identical.

Also in this pass: `design_turn.dry_run_se` skips `relax_chain` explicitly
(the item's "neither runs in the pure dry-run" rule) — without that branch it
falls through to `apply_ops`, which knows only the pure table and would report
the op as *unknown* in a proposal. **`join` still has that bug** and is
untouched here.

### Slice 2 pass C built 2026-09-29 — `fold_layout` and the fold findings

`precis_se/chain/fold.py`: the `fold_layout` op (`HANDLER_LEVEL_OPS`, now 7 —
the first entry intercepted for an **optional dependency** rather than for the
store; it is store-free) plus the fold findings, appended through
`chain/findings.py` (`supersedes` untouched — nothing pure measures a fold).
Slice 2 is complete in the tree; the deploy half is NOT (below). Measured,
with the command in each line's own terms:

- **The hairpin criterion holds exactly.** `fold_layout` on `GGGGAAAACCCC` →
  MFE `((((....))))` at −5.40 kcal/mol → 1 helix block `hp.h0` of 4 units, 1
  strand, 2 antiparallel domains both `[0, 4)`, `loop_before_nt = 4` on the
  second, and `derive_pairing` → 4 offsets, all `PAIRED`, all `W-W-cis`, 0
  singles and 0 conflicts. The domains carry `geometry='W-W-cis'` explicitly:
  every pair an MFE fold makes is cis Watson-Crick (the family that also
  carries the G·U wobble), so the derived pairing can state a family instead
  of leaving it undeclared — which is what makes the criterion's "4 `W-W-cis`"
  readable off `view='chain'` rather than inferred.
- **A dot-bracket decomposes totally, for the shapes covered.** One maximal
  *stack* (consecutive `i+1`/`j-1` pairs) = one helix; its two sides are the
  strand's two antiparallel domains; the domains sort by sequence position and
  **tile the sequence exactly**, so the unpaired gaps between them are the
  loops and the stored sequence's own length accounting stays exact. Covered:
  a hairpin, a multiloop, and any nesting of them.
- **What it refuses, and why refusal beat emitting.** Zero unpaired
  nucleotides between two consecutive domains — a bulge, a one-sided internal
  loop or a coaxial stack — is refused **naming the structure**, because the
  nominal placement puts each helix a helix-spacing apart and a 0-nt loop
  there is a *crossover* claim, not a stack. That refuses many real folds
  (`GGGGAGGGGAAAACCCCCCCC` → `((((.((((....))))))))` is refused;
  `GGGGAAAACCCCAAAAGGGGAAAACCCC` → `((((....((((....))))....))))` is laid out
  as 2 helices/4 domains/3 loops). The follow-up that lifts it is a *coaxial
  stacking* placement — put the second helix on the first's end instead of
  beside it — not a change to the decomposition. Also refused: an unpaired
  5'/3' tail (this model has no record for one — a loop exists only BETWEEN
  two domains), a fold with no pairs, a strand that already routes domains,
  and > 10 000 nt (`MAX_LAYOUT_NT`; scaffold length is allowed here, unbounded
  length is not).
- **The placement is nominal and says so** in the echo: straight helices along
  `+z`, stacked `+x` at `HELIX_SPACING_M`. `relax_chain` is what settles it.
- **ViennaRNA's parameters are RNA's, and a DNA fold is an approximation.**
  RNAlib's DNA parameter set is loaded as *global process state*, which a
  handler cannot safely mutate, so `T` is read as `U` under the Turner RNA
  model and every message carrying a fold says so.
- **A fourth rule, `chain_fold_skipped` (info)**, beyond the three the spec
  named — the same argument as `chain_malformed`: a 6 kb scaffold reads as
  fold-*checked* unless something says it was skipped. Measured: a random
  6 000 nt scaffold produces exactly one `chain_fold_skipped` naming
  `6000 nt is past the 200 nt` bound, and zero `chain_fold_disagree`. The
  skill's finding count is therefore **fifteen**, not fourteen.
- **`chain_offtarget` is a 6-mer hash index, reported at 8 nt.** The index
  granularity (6) is what makes it O(total length); the reporting threshold is
  `OFFTARGET_MIN_NT = 8`, the criterion's own number, because a 6-mer recurs
  by chance about every 4 kb² of pairwise sequence. Seeds containing any
  *intended* pair (`derive_pairing`, so "intended" means what the rest of the
  tier means) are dropped whole, then survivors extend outward while still
  complementary and still unintended. Measured: a 24 nt scaffold + two 12 nt
  staples with a planted 8-nt staple–staple complement → exactly **1** row,
  `st0[…] ↔ st1[…]`, 8 nt; the same design without the plant → **0** rows, so
  the declared duplexes are genuinely subtracted rather than the threshold
  hiding them. A random 6 kb scaffold → 171 chance runs ≥ 8 nt, reported as
  the 10 longest (12, 11, 11, …) plus one row naming the remaining 161; the
  whole pass takes 0.02 s.
- **`chain_fold_unavailable` is one row for the design**, not one per strand,
  and the off-target scan runs anyway (it needs no library). Measured over
  `view='drc'` on a 2-strand design with `RNA` unimportable: the body contains
  `chain_fold_unavailable` exactly once and still renders every other
  section.
- **The unavailable branch is tested unconditionally** by putting `None` in
  `sys.modules['RNA']`, which makes the real `import RNA` raise `ImportError`
  — so the test exercises `fold.rna_module`'s own `try`/`except` rather than a
  monkeypatched stand-in, and it runs in a container that has no ViennaRNA.
  Everything needing the real library `importorskip("RNA")`.
- **`RNA` is NOT importable in the dev image** (measured: `scripts/test
  tests/test_se_chain_fold.py` → 12 skipped), so those 12 were proven on the
  host venv instead (`uv run pytest tests/test_se_chain_fold.py` → 21 passed,
  2 failed only on the host's missing `fastapi`, 2 skipped for want of a DB).
  The dev image needs a rebuild for CI to exercise the installed branch.
- `mypy` needed `"RNA"` in pyproject's `ignore_missing_imports` list —
  ViennaRNA ships no stubs and is absent from the gate image.

**The deploy gap is real and untouched** (verified, not assumed): the `[chain]`
extra reaches **nothing**. `deploy/roles/mcps/tasks/main.yml` installs bare
`'precis-mcp @ git+…'` into `/opt/mcps/venv` (no extras at all — calc/mermaid
are core); `roles/precis_web` installs `[web,pcb,estimate]`;
`roles/precis_worker` installs `[paper]` (+`catalyst` on the plugin host);
`roles/precis_embedder` `[embed]`. So the session/serve MCP will report
`chain_fold_unavailable` forever and `fold_layout` will raise `Unsupported`
until `chain` is added to the mcps role's install line. The `UV_WITH` bridge
the Target section names is `scripts/test`'s (`scripts/test:263`), a TEST-time
escape hatch — there is no UV_WITH anywhere under `deploy/`, so that half of
the sentence describes the dev image, not prod. A promotion to core would also
need `viennarna -> RNA` in `scripts/lib/check-core-deps.py`'s
`IMPORT_NAME_OVERRIDES` (pass B1 already recorded this).

### Slice 2 rulings on the three calls the passes escalated (2026-09-29)

- **`chain_fold_skipped` stays, so slice 2 ships fifteen rules, not fourteen.**
  The brief expected fourteen; pass C added a fifteenth because "a 6 kb
  scaffold is visibly skipped, not silently" has nowhere else to live — the
  DRC table *is* the report, and folding the skip into
  `chain_fold_unavailable` (the library is present) or `chain_fold_disagree`
  (nothing was compared) would make either row state something untrue. Same
  argument `chain_malformed` won on in slice 1: a thing that was not checked
  is its own fact. Decided.
- **The settle does NOT write its stretch into a segment's envelope.** Pass B2
  re-imposes the body's nominal length on write-back, so the stored
  `cyl:…h<len>` stays exactly valid, and asked whether the settled
  (~1 % stretched) length should go in instead. It should not: the duplex
  length is a fact about the motif, and the stretch is an artefact of the
  kernel's rigid spring being a penalty rather than a constraint
  (`precis_chain.relax`'s own docstring says EV is a penalty, not a
  projection). Writing it into geometry would let a realizer read numerical
  slop as physics. Decided.
- **The private-Rodrigues duplicate is gone.** `precis_chain.relax` now
  exports `carry_rotation`, delegating to the batched `_axis_rotations` the
  settle uses internally, and `chain/relax.py` imports it instead of
  reimplementing it — the two cannot drift, which a hand copy guarantees they
  eventually would. Decided.

**The kernel's `tol`/`max_step` defaults are scale-bound, and this is now in
its docstring.** Every *stiffness* `relax_bundle` takes is in the caller's own
units, but `tol=1e-6` and `max_step` (5 % of a body length) are not: at
metre-scale nucleic-acid coordinates every force is ~1e-9, below `tol`, so the
settle returns `converged=True` at step 0 having moved nothing, and `max_step`
≈ 3e-10 could not cross a 4 nm gap in 500 steps regardless. A silent no-op, not
an error — the single most load-bearing decision in `chain/relax.py` is that it
builds its bundle in **nanometres** and converts back.

**Two numbers in the acceptance criteria were measuring something other than
what they say.** The relax criterion's "two helices … from 6 nm" is the *axis*
separation; the exits start 4.908 nm apart, because each sits 1 nm off its axis
at a groove-asymmetric azimuth (pass A). Settled: 1.875 nm against the stated
≤ 1.9 nm, whose floor is the loop's own contour at `(2+1)·0.63 = 1.89 nm` — so
that criterion passes with 0.6 % of margin **by construction**, and would have
been unsatisfiable had it been written 2 % tighter. The rectangle budget is the
opposite kind of stale: 0.42 s against 30 s, for 192 bodies / 168 hinges / 119
loop springs.

**The deploy gap is closed here, and the item's description of it was wrong.**
`deploy/roles/mcps/tasks/main.yml` installed bare `precis-mcp` into the session
MCP venv, so prod would have reported `chain_fold_unavailable` forever — that
line now installs `precis-mcp[chain]`. The item's Target section also named a
"UV_WITH bridge": there is no `UV_WITH` anywhere under `deploy/`. The only one
in the tree is `scripts/test`'s test-time escape hatch, so that clause was
always about the dev image, not prod. The dev-image half is real and still
open: ViennaRNA is in the `dev` group but the image has not been rebuilt, which
is why 12 of `tests/test_se_chain_fold.py`'s 25 tests skip in-container (all 12
verified on the host venv against ViennaRNA 2.7.2).

**Pass A was gated after all, and it passed.** The pass-A log above says main
carried three unverified layers and names the `chain_loop_short` 98 pm `tol` as
the prime suspect for a red gate. `check.yml` runs on every push to main, and
it ran on `b47faaf0` (pass A included): **15317 passed, 1 failed**. The single
failure was `precis-hexfold-help.md` with an H2 section at 4221 chars against
the 4000-char skill-chunk budget, alongside a `lint` ruff-format drift in a
blocktree web test — neither one chain, both since fixed on main
(green at `2c5b28d6`). The 98 pm tolerance survived a real Linux gate; retire
that suspicion rather than carrying it into slice 3.

---
status: draft
title: se region property layer — per-region non-geometric properties (hydrophobic, charge, field, optical) as taxonomy-measurand measures on selector-addressed regions; a pocket is a named set of regions and its spec is a defined class
pillar: 3d-design
prio: high
model: opus
blocked-by: term-taxonomy
---

# se region property layer

## Motivation / why

Reto's stated 3D goal (product-plan review, 2026-09-30) is a support
structure for an LLM to reason hierarchically about space, motion,
assembly, **charge, field and light**: "here be pocket, this side
hydrophobic, then negative, then positive, 1 nm long 0.2 nm wide with
shape X, backbone on the back — then groups are picked and joined until
things work."

The se model carries geometry, ports, joints, loads and *geometric*
measures, and nothing else. Peer session `unicycle` (se-3d-viewer owner):
"per-region non-geometric attributes — hydrophobic/charge/field/optical
on a face or pocket — the se model carries no such layer, so 'this side
hydrophobic, then negative' has nothing to attach to and the viewer can
only render what the model carries … three of Reto's six reasoning axes
are unbuildable from here." Peer session `hexa`: hexfold is pure sp²
topology with no per-atom chemistry channel; pocket *extraction* is on
its horizon (spec §28.8), patterning is on no item. The one place the
picture exists is `rotary-ratchet-valve.md` (attracting/rejecting arcs,
pocket extraction, arc-aware complementarity) — as one device's tooling.
`precis-se-atomic-help` states "No charge, optical, or simulation views."

**Decided with Reto 2026-09-30:** this is *not* a new object. It is three
extensions of machinery that exists — the se measure (`add_measure`
already has value/min/max, `strength hard|soft|gauge`, `origin`, and a
`datum` selector grammar resolved through the cad primitive at read time,
with a band-first mismatch check), the taxonomy (`term-taxonomy.md`), and
the measures table (`measures-substrate.md`). Granularity: **all three
(body, surface/edge, atom sites) through one selector**; per-body is the
degenerate selector.

## In scope

1. **Measurands come from the taxonomy**, not from se's unit enum
   (`m|count|ratio|deg`). `add_measure` accepts `measurand: <taxon handle
   or slug>`; the unit and SI vector come from the node. Seed nodes under
   the `measurand` root (all `proposed`, per `term-taxonomy.md`): contact
   angle · surface charge density · net partial charge · dipole moment ·
   H-bond donor count · H-bond acceptor count · electric field magnitude
   at a point · absorption maximum wavelength · hydrophobicity index
   (categorical `value_type`). Existing geometric measures keep working
   unchanged (the enum maps onto four seeded nodes).
2. **Selectors widen.** The v1 datum vocabulary (`frame` · `port:<name>` ·
   `face:<instance>.<tag>` · `axis:<instance>` · `face:largest` ·
   `face:normal=…` · `face:perp=assembly`, `src/precis_se/datums.py`)
   gains: `patch:<instance>.<tag>@<u>,<v>+<w>x<h>` (a bounded region on a
   face, dimensions in the design's unit), `ring:<instance>.<tag>` (an
   edge or rim loop), and in atomic mode `sites:<block>/<seam>/s<i>..s<j>`
   and `atoms:<block>[<indices>]` (hexfold's existing `<seam>/s<i>`
   addressing). Resolution stays a read-time feature lookup, never an
   optimiser DOF.
3. **A pocket is a named block feature**: `add_pocket` — `block`, `name`,
   `shape` (a cad primitive or an atomic hull), `regions[]` each
   `{selector, measures[]}`. The pocket's regions are ordinary measures
   with the widened selectors; the feature only names the set and holds
   the shape. `view='pockets'` lists them with each region's spec and,
   when computed, realised value and mismatch.
4. **Realised values land in `measures`** (`measures-substrate.md`):
   `subject_ref_id` = the se design, a new `subject_selector text` column
   carrying the selector string (this is the same slot as the design
   notes' `measurement_arg`), `tier='computed'`, `method` = the fidelity
   rung that produced it (`template | analytic | surrogate | full_solve |
   dft`, per `multiscale-design-system-spec.md` §1.4), `derived_from` the
   atom-level inputs. First computers, cheapest first: partial charges by
   a Gasteiger-class analytic rung; contact angle by a group-contribution
   surrogate over exposed moieties; field magnitude by a point-charge sum
   (analytic); DFT rung deferred to `precis_dft`. Each computer is a
   registered check the same way `validate()` findings are.
5. **Spec versus realised.** A region spec is an se measure with `min`/
   `max` and `strength`; the realised value is the `computed` row; the
   mismatch note is the existing band-first rule. `strength: hard` on a
   region measure makes its mismatch a `validate()` error.
6. **The pocket spec is a defined class.** The set of region constraints,
   canonicalised and hashed, is a `class` node per
   `class-lattice-similarity-spaces-and-laws.md`; candidate groups (hexfold
   catalogue entries, substituent tiers, library parts) are members
   `yes | no | unknown`. This item *emits* the class; the membership query
   and the pick-and-join loop are `se-intent-to-realize-loop.md`.
7. **Viewer contract.** The model exposes regions and their measures on
   the existing block read (`view='ops'` round-trips them); rendering is
   `se-3d-viewer`'s (its Horizon 1, `se-pick-hierarchy.md`, is the same
   surface). Nothing in this item touches `precis_web`.
8. **Skills.** `precis-se-help` §Ops gains the measurand/selector/pocket
   lines; `precis-se-atomic-help` drops "No charge … views" once (4) has
   one computer live.

## Explicitly NOT in scope

- A per-atom force field, MD, or any dynamics — the computers are static
  property estimates at a fidelity rung.
- The membership query, per-axis nearest-neighbour and the pick-and-join
  loop (`se-intent-to-realize-loop.md`).
- The class lattice itself (`class-lattice-similarity-spaces-and-laws.md`).
- Motion/kinematics (`se-bearing-kinematics-check.md`).
- Electrostatics as a *field solve* (spec §6.1 open item 3); only the
  point-charge analytic rung ships here.
- Rendering (viewer) and the DNA anchor domains that consume the
  selectors (`se-chain-wrap-around-part.md`).

## Acceptance criteria

1. `add_measure(block=…, measurand='surface charge density', datum='patch:…', min=-1.0, max=-0.5, strength='hard')` round-trips through `view='ops'` and appears in `view='pockets'` when the patch belongs to a pocket.
2. A geometric measure written with the legacy `unit='m'` form is unchanged in every existing test (`tests/test_se*`).
3. On the rotary-ratchet-valve fixture, `add_pocket` with two arcs (attracting/rejecting) reproduces the arcs `rotary-ratchet-valve.md` describes, and one computed row per arc lands in `measures` with `subject_selector` set, `tier='computed'`, `method` naming the rung.
4. A `hard` region measure whose computed value falls outside its band is a `validate()` error naming the selector.
5. The pocket's constraint set is emitted as a `class` node with a stable hash; re-emitting the same spec yields the same node.
6. `get(kind='se', view='pockets')` on a design with no pockets returns an empty list, not `Unsupported`.

## Target + blast radius

`src/precis_se/handler.py` (ops `add_measure`, new `add_pocket` /
`set_pocket` / `remove_pocket`, `view='pockets'`), `src/precis_se/datums.py`
(selector grammar), `src/precis_se/drc.py` (hard-band error), a new
`src/precis_se/properties/` package for the computers, one forward-only
migration adding `measures.subject_selector` (after `measures-substrate`
lands; coordinate the number against the prod ledger), taxonomy seed rows
for the measurands, `precis-se-help` + `precis-se-atomic-help`. Post-deploy
check: existing prod se designs (`unicycle-c1`) read back unchanged.

Two guards the builder hits on the first run (se-3d-viewer owner,
2026-09-30, verified against origin/main):

- `tests/test_se_ops_export.py` carries a parametrised `_VERDICTS`
  totality map: every field of `SeBlock`/`PortSpec`/`MeasureSpec`/… must
  be classified `op | gap | derived` or the build is red with
  instructions. New pocket/region fields on `SeBlock` need one line each;
  any non-design field also needs `persist._BLOCK_TRANSIENT`.
- The ops export is final-state-only and never emits `remove_*`; a new
  `remove_pocket` needs an entry in `ops_export.NOT_CARRIED`, the single
  tuple the docstring and the rendered header both print. Without it the
  gap is silent.
- `view='pockets'` will be one big H2 section, exactly the shape gr458393
  truncates on a one-shot `precis tools` read (pagination never falls
  back past the section level); land that fix first or paginate the view
  by pocket.

## Open questions / decisions log

- **[decided 2026-09-30, Reto]** Three extensions of existing machinery,
  not a new property object.
- **[decided 2026-09-30, Reto]** Granularity: body, surface/edge and atom
  sites, all through one selector.
- **[decided 2026-09-30, Reto]** Pocket specs are defined classes in the
  term-taxonomy lattice, shared with the paper graph's scenario classes.
- **open** — `subject_selector` on `measures` versus a `subject_path` that
  also serves papers (figure/table anchors already use `links.meta.span`);
  the extraction session's `measurement_arg` (position, entity) suggests
  one column with a small grammar. Decide when `measures-substrate` ships.
- **open** — hydrophobicity: contact angle (measurable, needs a surface)
  versus a categorical index (what the LLM actually says). Seed both;
  the class constraint may use either.

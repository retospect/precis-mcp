---
status: draft
title: estimate kind — millisecond chemistry workup panel (argue without sims, to set up sims)
prio: normal
---

# `estimate` kind — millisecond chemistry workup panel

## Motivation / why

Quest agents argue mechanism — d-band shifts, spin, "relativistic 5d
contraction", orbital gating — with **zero electronic-structure observables
in the system**. autocatpath's working calculators are MLIPs (energies +
forces only); the QE/VASP layer (`autocatpath/dft.py`) is a verification
backend and extracts no DOS/charges/magmoms. So every mechanism sentence in
the qu164903 dossier is an inference from element identity + energies:
plausible-sounding, unfalsifiable in-system.

Second failure the same tool fixes: sims get dispatched blind. The campaign
burned dispatches on a symmetry-duplicate pair (st245406≡st237458, caught
only by later audit) and on an unbonded H floating 4.1 Å above the surface.
Both are millisecond-detectable pre-dispatch.

Goal (Reto, 2026-08-26): let the model argue quickly with undergrad-ish
non-ML methods — *in order to set up sims* — and make every such estimate a
citable ref.

## Shape: one kind, existing verbs, one panel — not 100 tools

The method zoo hides *inside* a tiered workup panel; the agent asks about a
thing, never picks a method.

- `get(kind='estimate', q='Pd Zr H')` — composition only → element-descriptor
  tier (no geometry needed).
- `get(kind='estimate', id='st<...>')` — full workup of a held structure.
- what-if: `args={'ops': [...]}` on a structure handle, **reusing the quest
  candidate ops vocabulary** (`set_element`, `add_atom`, frac coords —
  `quest/compute.py`) to build the mutant in ASE in-call.
- `view='compare'` between two workups — the core argument form ("what did
  Zr *do*" = doped-vs-pristine delta).
- Depth views: `shape`, `orbitals`, `spin`, `kinetics`, `card` (SVG orbital
  diagram / 3Dmol carve-out via the registered card-variant synthesis path).

Results cache by hash(inputs, method versions) and mint an `es` ref with a
universal handle → citable in dossiers, linkable `derived-from → st…`.
Requires registering a handle code (plugins currently register **none** —
`handle_registry.PLUGIN_GROUP` exists unused; same gap as
`computed-pathways-cannot-be-cited-as-claim-evidence.md`).

Default panel = every ms-tier row eagerly + drill-down hints inline (the
`precis_pathway/toon_views.py` pattern). Two rows pay for the kind alone:

- **sanity/dedup line**: spglib site/symmetry check + pymatgen
  StructureMatcher against the quest tried-set + geometry lint (unbonded
  atoms, absurd bond lengths) — before any dispatch.
- **BEP row**: Brønsted–Evans–Polanyi scaling fitted to the campaign's *own*
  measured barriers; the prediction pre-registers the hypothesis branch, the
  residual is the post-hoc anomaly detector.

## Toolbox

Already installed (precis + catpath trees): `ase` (neighborlist + covalent
radii → coordination/strain/adsorbate height; `ase.build` what-if
constructor; EMT calculator — real effective-medium theory, Ni/Cu/Pd/Ag/Pt/Au;
`ase.thermochemistry`; `ase.data.ground_state_magnetic_moments`), `spglib`,
`scipy`/`numpy` (a ~200-line extended-Hückel on cluster carve-outs +
Newns–Anderson/d-band arithmetic), `networkx` (steady-state microkinetics
over the reaction network: Eyring rates, coverages, per-step residence
times), `rdkit` (adsorbate/side-product fragments: hybridization, bond
orders), `matplotlib` (cards), `chgnet` extra (**per-site magmoms predicted
as a byproduct — the ms spin picture; unread today**; also single-point
sanity evals).

Approved to `uv add` (Reto, 2026-08-26; all resolve on macOS arm64, verified):
`mendeleev` 1.2.0 (element property DB), `pymatgen` 2026.5.4
(ChemEnv/CrystalNN coordination-polyhedron naming + CSM, bond valence,
StructureMatcher), `tblite` 0.7.0 (GFN1/2-xTB, Z≤86 covers Pd/Zr/Nb/Ta:
Mulliken charges, Wiberg bond orders, HOMO–LUMO, spin densities on cluster
carve-outs, ~1 s).

Vendored data, not deps: Hammer–Nørskov d-band centers + V²ad couplings,
Harrison tight-binding tables, extended-Hückel (Alvarez) parameters →
`src/precis/data/`. CHE pH shift (59 meV/pH per proton-coupled step) is a
formula.

## Blind 3D design — the sharpest version of the problem (prod-measured)

The tool-less tick designs structures by emitting raw JSON ops — `add_atom`
with **guessed fractional coordinates**, `set_element` against atom labels
derived from an index-arithmetic hint in the prompt. It never sees the built
structure; errors surface a full tick later. qu164903's chunks (n=4,658):
44 mention rejected/refused proposals, 43 floating/unbonded atoms, 37 build
breakage, 36 discuss coordinates — including ticks that **searched the
literature for octahedral/tetrahedral interstitial coordinates** ASE
computes exactly, and the "corner saga" (translation-image duplicates
narrated as chemistry) that left a permanent PBC-ground-rules scar block in
`tick.py::_reaction_context`. The validated on-surface height z=0.66 is
trial-and-error folklore carried in prose.

Two-part fix here: (1) the panel's build-preview/sanity row (lint + dedup)
for anything expressed as ops; (2) deeper — extend the proposal vocabulary
in `quest/compute.py` with **site-symbolic ops** ("H at fcc hollow adjacent
to dopant", "octahedral interstitial below aPd28"): ASE+spglib enumerate and
name adsorption/interstitial sites deterministically, code resolves symbol →
coords, and coordinate guessing exits the model's job entirely. The
estimate panel names sites in the same vocabulary, closing the loop.

## Epistemic grade

Estimates are **hypothesis-generators, inadmissible for rulings**. The
ladder: estimate (ms) → MLIP sim (min) → QE autopsy (h) → literature. Panel
rows carry method + grade; an `[es…]` cite is visibly estimate-branded.
Validation gate before trusting in arguments: the semi-empirical layer must
reproduce knowns the campaign already measured (Au-vs-Pt d-band ordering,
d¹⁰ weak-interaction pattern) — that validation is itself a citable finding.

One router skill `precis-estimate-help` (question-shape → view), following
`precis-pathway-help`'s argue-with-data framing.

## Companions

`quest-dossier-dialectic.md` (the consumer of `[es…]` cites),
`computed-pathways-cannot-be-cited-as-claim-evidence.md` (handle-code +
evidence-edge sibling).

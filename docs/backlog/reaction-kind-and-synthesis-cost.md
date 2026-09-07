---
status: draft
title: reaction kind + synthesis cost — a sourced reaction-fact store, and cost as a graded vector over routes
prio: high
model: opus
---

# `reaction` kind + cost-of-synthesis

Design session 2026-09-06 (Reto + agent), immutable-watching-fairy worktree.
Reto's framing:

> I want to have chemistry synthesis kind in precis. I want cost of synthesis
> (how complicated it is, yield) eventually. And it should be extendable! From
> papers (with cites, nanopubs). But start maybe with existing library. This is
> to keep synthesis stuff we dream up implementable if we want that (with
> existing chemistry).

Best-practice grounding: `perplexity-research:328550` (synthetic accessibility
and cost-of-synthesis metrics, open reaction databases and their licences,
canonical reaction schemas, route-scoring frameworks).

## The headline: this is one new kind, not four, and the other three exist

The request decomposes into four capabilities. Three already have homes, and
building a monolithic "synthesis" kind would duplicate all three:

| capability | home | state |
|---|---|---|
| engine-planned retrosynthesis graph | `route` (`src/precis_chem/`) | shipped, dark behind `chem.enabled`, only the `stub` engine live |
| authored synthesis *order* with step conditions | `make` (mig 0154) | shipped 2026-09-05; its design doc names bulk synthesis as one of its two make-orders |
| computed scores attached with provenance + staleness | attached-models layer (`analyzed-by`, mig 0153) | shipped v1 2026-09-05 |
| **a reaction as a reusable, citable, sourced fact** | **nothing** | **the gap** |

So: **add a `reaction` kind, and build cost as a graded vector over the routes
and make-trees that already exist.** Do not grow a second chem surface —
`chem-name-lookup-verb.md` and `composable-pipeline-kind.md` both already flag
that risk, and this item is the reconciliation point.

The gap is worth stating precisely. `make` holds step conditions as free-form
meta *per tree*: not reusable across trees, not searchable, not sourced. `route`
holds engine-guessed conditions and no yield at all. Neither can answer "what
yield has this transformation actually been reported at, and by whom" — which
is exactly what "extendable from papers with cites" asks for.

## `reaction` is `material` with a transformation as the entity

The shape is already in the tree twice (`material` mig 0092, `component` mig
0093 which copies `material_values` verbatim). A third copy is the house
pattern, not a new invention:

- **entity** — slug ref. `meta` carries the **canonical reaction uid** (see the
  Python-surface section — LinChemIn's `ChemicalEquation.uid`, measured
  canonical across SMILES spelling and reactant order; *not* RInChI, which the
  rdkit wheel does not expose), the atom-mapped reaction SMILES
  (`reactants>agents>products`), and a reaction-class label. RInChI stays an
  optional interchange field, populated if a binding turns up.
- **`reaction_properties`** — typed growable registry, `core`/`proposed` tiers,
  canonical unit + dimension, exactly `material_properties`. Core seed:
  `yield`, `temperature`, `time`, `pressure`, `catalyst_loading`, `solvent`,
  `scale`, `ee`, `atom_economy`.
- **`reaction_values`** — the fact table, `material_values` verbatim:
  `value_num/low/high`, `conditions` jsonb, `maturity ∈ {commercial, lab,
  speculative}`, `method`, `source_ref_id`, `source_chunk`, `as_of`.

**The load-bearing property is inherited, not designed: many rows per
(reaction, property) is the point.** Twelve reported yields for one
transformation across twelve papers *is* the answer; nobody picks a canonical
number at write time. `material`'s docstring already says this ("the handbook
shows the spread across sources/conditions") and it is precisely what a
synthesis knowledge base needs — a single "yield: 72%" field would be the lie.

### Why this sidesteps the citation blocker rather than inheriting it

`taproot.hub.EVIDENCE_SRC_KINDS` is `{paper, patent, edgar, datasheet}`, and
computed artefacts cannot ground claims at all — prod-measured, every link
touching a `pathway` ref is `related-to`, zero evidence edges
(`computed-pathways-cannot-be-cited-as-claim-evidence.md`). A synthesis kind
modelled as a *computed* artefact walks straight into that wall.

Modelled as a **sourced-value store** it does not. `material`'s
`_SOURCE_KINDS = ("paper", "datasheet")` already gives chunk-precise
provenance per value row; extend it with `patent` (which is where the reaction
literature actually lives) and the "extendable from papers with cites" leg
works on day one with no taproot change. Claims *about* a reaction still cite
the paper chunk through the normal hub path; the reaction ref carries the
numbers. Mixing both on one hub is desirable, not a problem.

**Register a handle code (`rx`) from day one.** Correction (2026-09-07 sweep):
`pathway` *does* have one — `precis_pathway/handles.py` registers `pw`, so the
plugin entry-point group is in live use, not unused. It is **`route` and
`protein`** that have no handle code (there is no `precis_chem/handles.py` or
`precis_bio/handles.py`), which is why neither can be cited in a draft. Do not
repeat that.

## Cost of synthesis: the `se-feasibility-and-cost` architecture, not a scalar

Reto has already decided this architecture one domain over, and the report
independently confirms it. From `se-feasibility-and-cost.md`:

> If we can not make it or we can not assemble/synthesize it it's a useless
> solution. If we can assemble it with binding and a lot of pain, it's a
> working solution but bad.

Same two tiers here:

**Hard — DRC.** *The route does not terminate in stock.* A branch ending in a
non-purchasable precursor is not a worse route, it is not a route. This is
already the open item in `make-tree-vs-design-tree.md` ("routes must terminate
in stock … the lint should say so"), and `RouteStep.in_stock` /
`RouteGraph.solved` already carry the signal — nothing consumes it. Emits an
error; a candidate carrying one is removed from consideration, not ranked last.

**Soft — a named vector in different units, never silently summed.** Each term
carries its provenance and the fraction of inputs actually grounded:

- **complexity** — free today. `RouteGraph.metrics` *is* the LinChemIn
  `routes_descriptors` row (`nr_steps`, `longest_seq`, `branchedness`,
  `convergence`, `cdscore`), and `ir.py` already calls it "the substrate for
  our own scoring". Nothing consumes it.
- **yield** — multiplicative: `Φ = ∏ yᵢ`. The correct cost roll-up is *not*
  `Σcᵢ/Φ` but

  ```
  CostScore = Σᵢ  cᵢ / ∏_{j≥i} yⱼ
  ```

  Material invested early is amplified by *every* downstream yield loss; late
  steps dominate final yield, early steps dominate cost. Getting this wrong
  is the standard error.
- **mass** — atom economy is computable from stoichiometry alone, so it comes
  cheap. **PMI and E-factor are not additive across steps** — cumulative PMI is
  a recursion through yields — and they need solvent and workup volumes we
  will not have early. Compute them when the data exists; report them absent
  otherwise, never estimated.
- **accessibility** — SAscore first (see below).

**The coverage line is the honesty mechanism.** Copy `component`'s BOM rollup
verbatim: `"unit_cost total: … — unit_cost: 4 of 7 leaves"`. A route with two
measured yields and five guesses must say so, or it reports a confident total
built from nothing. This is the same discipline as `pathway`'s pooled
uncertainty and the same rule the whole tree already follows.

A monetised scalar may be derived **on request with explicit weights**. It is
never the primary artefact, and a weight set is a recorded decision — same rule
as the se objective vector and the quest `rubric_composite` (weights are
human-set per quest; the agent may not tune its own objective).

### Accessibility scores: start with SAscore, and know what it is

The report is blunt about the state of this literature, and the caveats matter
more than the ranking:

- **SAscore** (Ertl/Schuffenhauer) — fragment frequencies over ~1M PubChem
  molecules minus a complexity penalty, scaled 1–10; r² ≈ 0.89 against
  chemists' judgement. **In RDKit, which is already the `[chem]` extra.** Zero
  new dependencies. This is the v1.
- **SCScore** — learned from a reaction corpus on the assumption that products
  are more complex than reactants. Measures *complexity*, not accessibility;
  a complex molecule with a robust industrial route scores badly.
- **SYBA** — Bayesian ES/HS log-odds, GPL, fully per-fragment transparent.
- **RAscore** — a classifier for "would AiZynthFinder find a route", ~4500×
  cheaper than running it. Inherits AiZynth's template and stock coverage
  wholesale: a molecule routinely made by chemistry outside that library scores
  inaccessible.
- **RB-SAScore** — splits SAscore's fragment term into a building-block score
  and a reaction-driven score. This is the fragment-level move
  `make-tree-vs-design-tree.md` already asked for ("a score on
  blocks/fragments, not just the whole").

**The finding that should govern our use of all of them:** SYBA's reported
advantage over SAscore *disappears* once SAscore's threshold is tuned
(6.0 → ~4.5). Threshold choice dominates method choice. Every one of these
encodes historical bias and penalises novel scaffolds — which is the exact
failure mode for Reto's stated purpose ("synthesis stuff we dream up"). So:
**these are soft filters and ranking inputs, never gates.** A low score
deprioritises; it never removes. Only stock-termination is hard.

Scores attach as **attached-model results** (`analyzed-by`, mig 0153) carrying
engine + version + validity scope, so they go stale loudly when the structure
changes. The hard rule from `attached-models-layer.md` applies unchanged: do
not ship attachment storage before the staleness design.

## Seeding from an existing library

**USPTO (Lowe) is the v1 seed and the only one that is unambiguously safe.**
**CC0 1.0 confirmed** against the figshare API — DOI
`10.6084/m9.figshare.5104873.v1`, "Chemical reactions from US patents
(1976–Sep2016)", v1 only, 1.495 GB of `.7z` (CML + rSMILES, grants +
applications). Atom-mapped reaction SMILES; yields where the patent stated
them. USPTO-50k for a bring-up slice, the full corpus after.

**But it is raw archives with no parser**, and that has a packaging
consequence. The maintained parser is AstraZeneca's `reaction-utils`
(`rxnutils`, Apache-2.0, active), which has the figshare download URLs
hard-coded and a real download→clean→structure pipeline. It pins
`numpy<2.0.0`, `rdkit<2024.0.0`, `urllib3<2` and caps at Python <3.13, and
pulls metaflow + dask. **It therefore cannot go in the main venv** — rung 2 is
a container or a one-shot offline ETL whose output is plain rows, not a
runtime dependency. (Its own docs already mandate a *second* env for the
atom-mapping stage, because rxnmapper's deps conflict with its own. Take that
as the warning it is.)

**ORD (Open Reaction Database) is second, and is the schema to align to.** Its
Protocol Buffers `Reaction` message — identifiers, inputs, setup, conditions,
notes, observations, workups, outcomes, provenance — maps almost one-to-one
onto the property-registry + value-table split above. Two of its design choices
are worth copying outright: it is **descriptive, not prescriptive** (record
what was done, not an idealised protocol), and every structured object carries
a free-text `details` escape hatch alongside its typed fields.

The reader is healthy: `ord-schema` 0.8.3 is Apache-2.0, very actively
developed, `>=3.11`, and torch-free (protobuf `<6` + pyarrow are the notable
pins). **But the licences differ in kind and this must be recorded, not
glossed:** the ORD *data* repo is **CC-BY-SA-4.0** — share-alike — where USPTO
is CC0. Share-alike on ingested facts is a governance decision above my pay
grade, and it is the reason USPTO goes first regardless of ORD's richer
conditions. The data is also no longer small or protobuf: ~1.26 GB, migrated
to **Parquet-only** in Aug 2026, held in git-LFS behind a Hugging Face mirror.

**Reaxys and Pistachio are excluded.** Both proprietary, neither
redistributable. Recording this as a decision rather than an omission: the
report's data-governance section is explicit that proprietary and open data
must be segregated so that models trained on the former are not shared in
violation of licence. Rhea (CC BY) and RetroBioCat are open and available if
biocatalytic routes ever matter; MetaCyc is academic-use-only.

**Reuse the ADR 0053 importer seam verbatim** — it was built for exactly this
and is the one piece of ETL architecture already proven in tree
(`structure/importers/__init__.py`): a pure adapter
`raw_record -> (entity, values, ExternalId)`, no I/O and no DB writes in the
adapter, a single store-side writer, `provenance='external'`, and external rows
structurally excluded from serving a compute cache hit.

**`ExternalId` = the canonical reaction uid** (LinChemIn's, per the
Python-surface section). That is what makes the store idempotent and genuinely
*extendable*: the same transformation arriving from USPTO and later from ORD
collapses onto one ref, keeping *both* value rows with their separate
provenance — which is the multi-row-per-property feature doing exactly the work
it was designed for. Pin the linchemin version beside the uid, the way
`cache_key` already pins `engine_version`; a uid scheme change is then a
visible re-key, not silent drift.

## The Python surface — measured, not assumed (2026-09-06)

Everything in this section was verified locally in this worktree on Python
3.13.5 (`requires-python >= 3.12`), not read off a package page.

**What the tree has today.** `chem = ["rdkit>=2023.9"]` is the *only* in-process
chemistry dep, deliberately out of `[all]`. `linchemin==3.2.0` and
`aizynthfinder==4.3.2` are pinned **container-only** (`docker/aizynth`,
`docker/normalizer`, both `python:3.11-slim`). Base deps already carry
`ase`/`spglib`; `[estimate]` carries mendeleev/pymatgen/tblite.

### Three findings that change the plan

**1. SAscore is free. Confirmed running.** The PyPI `rdkit` wheel (2026.03.4)
ships `Contrib/SA_Score`; `sascorer.calculateScore` works off
`RDConfig.RDContribDir`. Aspirin 1.58, caffeine 2.30. **Zero new dependencies**
— rung 3 costs nothing but the code.

**2. Atom economy is rdkit-only, but not off the reaction template.**
`ReactionFromSmarts` yields *query* mols; `Descriptors.MolWt` raises
`Pre-condition Violation … calcImplicitValence` on them. Parse the component
SMILES separately instead. Validated against the textbook cases: Fischer
esterification 83.0 %, Diels–Alder exactly 100.0 %. No library needed.

**3. LinChemIn does not have to stay in the container — and this is the big
one.** `linchemin==3.2.0` clean-resolves on Python 3.13 to a 30-package
closure, **torch-free and tensorflow-free**.

The marginal cost is far smaller than that suggests. Diffed against `uv.lock`
(287 packages), **24 of the 30 are already locked** — rdkit, numpy, pandas,
scipy, scikit-learn, pydantic and the rest are all here already. The genuinely
new packages are **six**:

```
cloudpickle · dynaconf · hdbscan · linchemin · pydot · rdchiral
```

Only `hdbscan` is a compiled wheel of any consequence. This is a much lighter
ask than `[estimate]`'s pymatgen/tblite tier, and it has no platform-marker
problem of the kind tblite needed. Licence is MIT (Syngenta).

One caveat to weigh: linchemin's release cadence is slowing — 3.2.0 is from
2025-05-06, seven releases total. It is not abandoned, but it is a single-
vendor package, so the wrapper should stay thin enough that the descriptor
computation could be replaced without touching the kind.

More importantly, `BipartiteSynGraph` accepts a bare list of reaction SMILES —
`[{'query_id': n, 'output_string': 'A.B>>C'}]` — so **a hand-authored route can
be scored with no engine at all.** Measured, on a 3-step route typed by hand:

```
roots  : ['CCOC(=O)CC']          leaves: ['CCBr', 'N#[C][Na]', 'O', 'CCO']
nr_steps 3 · longest_seq 3 · convergence 1.0 · branching_factor 1.0
cdscore 0.47 · simplified_atom_effectiveness 0.7
```

That is the entire complexity term of the cost vector, computed in-process, for
a synthesis nobody planned with a planner. **This is precisely Reto's stated
use case** ("synthesis stuff we dream up"), and the earlier assumption — that
`RouteGraph.metrics` only ever gets populated by the dark container path — is
wrong. It is why every stub route has empty metrics today, but it need not be.

`translate` also accepts `networkx`, `askcosv1/v2`, `az_retro`, `ibm_retro`,
`reaxys`, `sparrow`, `iron`, `pydot` as inputs — so a `make`-tree lowered to a
networkx DiGraph is a supported entry point too.

Two operational gotchas, both already ground-truthed and still true: linchemin
refuses to import `facade` until `$HOME/linchemin/settings.yaml` exists (run
the `linchemin_configure` console script; the dir is `$HOME/linchemin`, *not*
`~/.linchemin`), and `packaging` is a missing transitive dep — declare it.
Correction to the older ground-truth note: 3.2.0's descriptor frame is **nine**
columns (`route_id` + eight metrics), not eight — `branching_factor` was
missing from that list.

### RInChI: the recommendation above was wrong, and there is a better key

The earlier draft made RInChI the `ExternalId` collapse key. **The PyPI rdkit
wheel exposes no RInChI at all** — `dir(rdChemReactions)` contains zero
InChI-related symbols (bundled InChI is molecule-level 1.07.3). This is not a
build flag: rdkit issue #1391 "Support for generation of RInChI" (opened 2017)
was **closed 2025-01-06 as `not_planned`**. That leg does not stand as written.

Nor is there a healthy alternative. Upstream `IUPAC-InChI/RInChI` has **zero
releases and zero tags**, ships under the non-OSI IUPAC/InChI Trust Licence,
and its "Python interface" is a hand-rolled ctypes wrapper around a
`librinchi.so` you build yourself; inchi-trust.org still lists RInChI **1.00
(2017)** as current. Exactly one PyPI package computes a RInChI —
`rxnsmiles2rinchi` 0.1.1 — and it does work (rdkit-only, correct
RInChIKeys), but:

- **it ships no linux-aarch64 binary**, hard-coding `linux/x86_64` for every
  non-Darwin platform. That is *precisely* the trap that forced tblite's
  platform marker in `[estimate]`, and this fleet has aarch64 nodes. It would
  be an import-time `OSError` on them.
- the publisher is anonymous (author email `your@email.com`), it bundles
  opaque prebuilt shared objects, its API takes `bytes` not `str`, and its
  convenience method splits on `>>` so three-part reaction SMILES with agents
  (`A>B>C`) are mishandled.

So RInChI is not merely unnecessary here — adopting it would import a known
platform break and a supply-chain question for no gain over the key below.

The replacement is better anyway, and it is already in the resolved dep set.
`ChemicalEquation.uid` is a **canonical** reaction hash — measured: the same
reaction spelled with reversed reactant order and different SMILES forms
(`CCBr` vs `BrCC`, `CCC#N` vs `N#CCC`) yields a byte-identical uid, and adding
a byproduct correctly changes it.

So: **`ChemicalEquation.uid` is the internal collapse key.** Its one weakness
is that it is a LinChemIn-internal hash — version-coupled, not a cross-
ecosystem standard — so pin the linchemin version alongside it exactly as
`cache_key` already pins `engine_version`, and keep RInChI as an *optional
interchange field* to be populated if a maintained binding turns up. Do not
block on it.

### What this implies for packaging

`route`'s complexity metrics and the whole soft cost vector become computable
in a plain `uv` install — no podman, no compute node, no `chem.enabled`. That
argues for a new `[synth]` extra carrying `linchemin` + `packaging`, lazily
imported in the handler (the `precis_estimate` pattern: module import never
touches it, so a `[synth]`-less venv still boots every other kind). rdkit-only
rungs (SAscore, atom economy) need no extra beyond `[chem]`.

The real planners stay in containers — they are heavy, and the image is
`python:3.11-slim` for a reason. That boundary is now about *engines*, not
about scoring.

## The agent-facing surface — what an LLM actually calls

The kind is only as good as the verb surface, so it is specified here rather
than left to the build. Everything below is the seven verbs; no new verb, no
new tool.

### Files this touches (core-kind route)

`handlers/reaction.py` · `store/_reaction_ops.py` + `store.py` ·
`migrations/0157_reaction_kind.sql` (0156 is current) ·
`utils/handle_registry.py` (`"reaction": "rx"`) · `dispatch.py` ·
**`tools/core.py`** · `data/skills/precis-reaction-help.md` +
`precis-overview` + `precis-toolpath-help` · tests incl.
`test_kind_totality.py` **and** `test_item_view.py` as a pair (they red in
sequence, and `--impacted` misses half).

### Record a reaction, then append sourced values

```python
put(kind='reaction', id='fischer-etoac',
    title='Fischer esterification → ethyl acetate',
    rxn_smiles='CC(=O)O.OCC>>CC(=O)OCC.O')          # uid + class derived

put(kind='reaction', id='fischer-etoac',
    property='yield', value=83, unit='%',
    conditions={'solvent': 'toluene', 'catalyst': 'H2SO4', 'reflux': True},
    maturity='lab', method='measured',
    source='paper:pa12345', chunk='pc67890')
```

Second call is the whole "extendable from papers" story: one sourced fact,
chunk-precise. Call it again from another paper and **both rows persist**.

### Read it — the spread is the answer

```python
get(kind='reaction', id='fischer-etoac')            # handbook page by property
get(kind='reaction', id='fischer-etoac', view='yield')
```

The render must show *every* reported value with its conditions and citation,
never a collapsed average. A single number here would be the lie the multi-row
schema exists to prevent.

### Find reactions

```python
search(kind='reaction', q='amide coupling')
search(kind='reaction', property='yield', min=80, maturity='lab')
```

### Score a route — including one nobody planned

```python
put(kind='route', id='my-target', target='<SMILES>',
    steps=['A.B>>C', 'C.D>>E'])                     # authored, no engine
get(kind='route', id='my-target', view='metrics')   # linchemin descriptors
get(kind='route', id='my-target', view='cost')      # the graded vector
```

`view='cost'` is the item's payoff and its render is load-bearing:

```
# cost → my-target
FEASIBILITY  ✖ not stock-terminating — 'CC(=O)N1CCC1' (step 3) is not buyable

complexity     nr_steps 4 · longest_seq 4 · convergence 1.0 · cdscore 0.31
accessibility  SAscore 3.8 target · worst precursor 6.1 (step 3)
yield          overall 0.34            [yield: 2 of 4 steps grounded]
mass           atom economy 0.61 · PMI — (no solvent data)
```

Four units, four lines, never summed; the bracket says how much of the answer
is real; the `✖` is the hard tier and is the only thing that gates. A missing
number renders `—`, never an estimate.

### Attach a reaction to a make-tree step

```python
put(kind='make', id='my-synthesis', text='amide coupling',
    meta={'reaction': 'rx4471'})
```

### The discipline the skill must teach

Mirrors `precis-pathway-help`'s argue-with-data framing:

- **Never invent a yield.** Absent renders absent. The coverage bracket exists
  so a confident total built from two numbers and five guesses is impossible.
- **The spread is the finding.** Three papers reporting 45/71/88 % is more
  informative than any average of them, and the conditions explain the gap.
- **Soft scores rank; only stock-termination gates.** SAscore penalises novel
  scaffolds by construction — using it as a gate would reject exactly the
  dreamed-up chemistry this kind exists to keep implementable.
- **Ground the lever before proposing it.** `search(kind='paper', …)` for the
  transformation, cite what you lean on, then record the value.

### One gap found while specifying this — do not inherit it (gripe 329157)

`material_values.method` (`measured|datasheet|dft|estimated|…`) has a store-op
writer but **no path from the agent**: `method=` is absent from both
`MaterialHandler.put` and the MCP `put` verb in `tools/core.py`. It is a dead
column from the surface, and `component` copied the shape verbatim.

For `reaction` that gap would be disqualifying rather than cosmetic:
`method` is what separates a hand-entered measured yield from a bulk
`patent-extracted` one, and the "N of M grounded" line is meaningless if an
imported number is indistinguishable from a curated one. **Wire `method=` end
to end (verb → handler → store) in rung 1**, and consider back-filling the
material/component surface while the context is loaded.

## Getting a route for a real target — generator vs checker

Measured 2026-09-06, because the answer to "how do we get a path for a complex
molecule" turned out to depend on deploy state nobody had checked.

**Current state: no retrosynthesis engine is confirmed live.** The gateway
(melchior) is correctly configured — `PRECIS_CHEM_ENABLED=1`,
`PRECIS_CHEM_ROUTE_NODE=castor` — and the AiZynth models are already staged on
the NAS at `/opt/nfs/shared/aizynth-models/` (config.yml + `.onnx`/`.csv.gz`/
`.hdf5`). But **castor was unreachable over SSH**, so neither the
`precis-aizynth` image nor the worker env drop-in could be verified, and
`get(kind='route')` on prod returns **zero routes ever minted**. The only live
engine is the deterministic `stub`, which is a round-trip test fixture, not a
planner. (Castor/pollux unreachability may be the known Spark sshd
banner-timeout rather than an outage — do not conclude the playbook failed;
conclude it is unverified.)

Shortest path to a real answer: reach castor, run `playbooks/43-aizynth.yml`,
mint one route. That is a playbook run, not a build — everything else is done.

**But a planner is the wrong thing to lean on for a genuinely complex target.**
AiZynthFinder is template-based MCTS: it finds only disconnections in its
USPTO-derived template library, terminating in *its* stock set. On
natural-product-like complexity it returns unsolved — that binary is literally
what RAscore was trained to predict. So the realistic path splits three ways:

1. **Literature first.** For a complex molecule the route usually already
   exists in a total-synthesis paper. `search(kind='paper', …)` over the corpus
   with the citation ladder is precis's genuine strength here, and it is the
   only one of the three that yields an *evidenced* route.
2. **Planner on the tractable fragments.** Decompose strategically by hand,
   then let the engine do what it is good at — "make this substituted aryl from
   buyables" — rather than the whole molecule.
3. **The LLM proposes the strategic disconnection; precis checks it.**

**Point 3 is the one this item enables, and it is the honest framing of the
whole design: precis is far better positioned as the _checker_ than the
_generator_.** An agent writes the steps as reaction SMILES and the system
answers immediately, with no engine at all: does every branch terminate in
stock (hard DRC), what are the descriptors (linchemin, in-process), what yields
have actually been reported for these transformations (the reaction store),
what is each intermediate's SAscore.

That is also why the reaction store matters more than the planner. **A planner
hands you a route with no evidence. The reaction store tells you whether anyone
has ever run those steps, and at what yield** — which is the difference between
a plausible-looking graph and something implementable.

### When the target does not exist yet — the case that governs the design

Reto, 2026-09-07: *"the molecules we aim for do not yet exist in literature or
elsewhere."* That deletes the literature-first leg above for the *target*, and
forces a correction to the exact-uid key proposed earlier.

**The reframe: novel molecules are made from known reactions.** The novelty is
in the assembly, not the chemistry. So precedent transfers at the level of the
*transformation*, never the molecule — which is exactly why a reaction store is
the right primitive and a route store is not.

**Correction to the earlier planner caveat.** Template-based retrosynthesis is
*generative*, not lookup: a template is a substructure pattern, so it fires on
molecules that have never been made. Verified in-process with rdkit alone — a
retro amide template applied to two never-synthesised targets:

```
CC(=O)NC1CCN(c2ncccn2)CC1        =>  CC(=O)O + NC1CCN(c2ncccn2)CC1
O=C(NCc1ccc(Cl)cc1)c1cnc2[nH]ccc2c1 =>  O=C(O)c1cnc2[nH]ccc2c1 + NCc1ccc(Cl)cc1
```

Novelty of the target is therefore *irrelevant* to whether a planner works. The
earlier caveat was about **complexity** — template coverage, stock termination,
and strategy (protecting groups, order, chemoselectivity) that templates do not
encode — not about novelty. A planner is in fact the *more* appropriate tool
for a novel target than corpus search is.

**The design gap this exposes.** Keying reactions by canonical uid answers "has
this precise reaction been run" — which for a never-made molecule is *always
no*, and therefore useless on its own. Precedent must also be indexed by
transformation.

**But template strings do not cluster — measured.** Two amide couplings
differing only in substrate produced *different* SMARTS, because rdchiral pins
the substituent environment per substrate (`[C;D1;H3:1]` for a terminal methyl
vs a generic `[C:1]` for a chain carbon). Template-as-hash-key is broken; do not
build on it. What works instead:

- **template as a _matcher_** — run the SMARTS against the target (demonstrated
  above, rdkit-only, no engine);
- **a coarse reaction class as the _aggregation key_** ("amide coupling"),
  which needs a classifier or ontology. NameRxn is proprietary; **RXNO** is the
  open reaction ontology to look at first.

So the store needs **two axes**: `uid` (exact identity, for dedup and for
collapsing imports) and `reaction_class` (for precedent transfer). The second
is what serves novel targets and was underspecified in rung 1 — add it as a
core property with the class-assignment path named, even if v1 populates it
only where the source dataset already carries a label.

**The deliverable for a novel target is a risk map, not a route.** With the two
axes in place the loop is: apply templates to the target → for each proposed
step query the store by class plus substrate similarity → report per-step
precedent coverage alongside the cost vector:

```
step 1  amide coupling         precedent n=214  yield 45–88 % (median 71)
step 2  Suzuki coupling        precedent n=88   yield 52–91 % (median 74)
step 3  ⚠ no precedent — this bond has not been formed this way
```

For a molecule nobody has made, *that* is the answer a chemist wants: not "here
is how to make it" but **"here is the plan, and here is precisely which step is
unprecedented."** It is the difference between a synthesis project and a
research project, and nothing in the tree can currently say it.

**Where precedent runs out, hand off — do not bluff.** A step with no matching
template and no class precedent is not a synthesis step, it is a *hypothesis*.
That is a boundary this codebase already has machinery and discipline for:
`estimate` for ms-tier mechanistic argument, `pathway`/autocatpath for barriers
on a proposed mechanism, the quest layer for running the experiment, and the
hypothesis-hub rules (evidence-free by design, falsifiable, never
pseudo-grounded). The unprecedented step should mint as a hypothesis, not be
scored as though a yield existed.

### The one thing to tell an agent about complex targets

Yield is multiplicative, so on long routes **topology dominates chemistry**. An
11-step linear route at 80 %/step returns ~8.6 % overall; the same 11 steps
arranged as two 5-step branches plus a coupling has a longest linear sequence
of 6, for ~26 % — roughly triple, from rearrangement alone.

This is exactly what linchemin's `convergence` / `branchedness` /
`longest_seq` measure, and why `view='cost'` must surface `longest_seq`
alongside `nr_steps`: for a complex target the *depth* of the tree is the cost
driver, not its size. An agent optimising step count is optimising the wrong
number.

## Step-level risk checks — what actually makes a path low-risk

Measured 2026-09-07 with rdkit only. This is the DRC layer from
`se-feasibility-and-cost.md` applied per *step*: one check registry, hard tier
vs graded tier, message quoting the headroom spent.

### Chemoselectivity ("wrong place to join") — solved, free, deterministic

**The number of distinct template matches _is_ the ambiguity metric.** No ML,
no engine, no 3D:

```
single amide        1 distinct disconnection
TWO amides          2   CC(=O)NCCN + O=C(O)c1ccccc1  /  CC(=O)O + NCCNC(=O)c1ccccc1
amide + lactam      2
```

Same query forward, over competing nucleophiles:

```
NCCc1ccccc1     aliphatic=1 aniline=0  -> OK
NCCc1ccc(N)cc1  aliphatic=1 aniline=1  -> AMBIGUOUS (2 sites)
NCCCCN          aliphatic=2 aniline=0  -> AMBIGUOUS (2 sites)
```

A step whose template matches in more than one place needs a protecting group,
a selective reagent, or a different order — and the checker can say so before
anything is proposed to a human. **This is the single highest-value check in
the item and it costs nothing.**

### Steric — needs 3D; the cheap graph proxy is a trap

A connectivity proxy looks tempting and **gets the textbook cases wrong.**
Measured, on substitution count at the reacting N:

- **v1 (α-substitution only):** neopentylamine scored **the same as
  n-propylamine**. Neopentyl is the classic hindered case.
- **v2 (α + β branching):** fixes neopentyl, still scores
  **2,6-dimethylaniline the same as isopropylamine** — ortho-aryl hindrance is
  not a branching count.

3D buried volume (`%V_bur`: ETKDG conformers → MMFF → Monte-Carlo occupancy in
a 3.5 Å sphere) orders them correctly:

```
methylamine 20.8 · n-propyl 28.3 · aniline 29.6 · isopropyl 33.1
neopentyl 33.9 · 2,6-Me2-aniline 38.4 · tert-butyl 39.1
```

2,6-dimethylaniline landing next to tert-butylamine is right — it is
notoriously hard to acylate. Cost is five conformers plus MMFF plus MC
integration, **rdkit + numpy only, both already dependencies**.

**Rule: use 3D or omit the number.** A graph steric score would silently
greenlight a neopentyl coupling, and the se-kind rule applies verbatim —
declared-but-unchecked physics is worse than absent physics.

### Named, not built: functional-group incompatibility

The third failure mode (conditions destroy something else in the molecule;
Grignard on a substrate bearing a ketone) is **not** solved by either check
above. It needs FG perception plus a curated compatibility matrix — which is a
`material`-shaped sourced registry, authorable from literature and citable.
Named here so it is not mistaken for covered.

### How they compose

Hard tier: no template matches, or the branch does not terminate in stock.
Graded tier, quoting headroom the way the pcb DRC does:

```
step 3  ⚠ 2 competing amine sites (expected 1) — needs PG or selective reagent
step 4  ⚠ %V_bur 38.4 at the nucleophile (soft ceiling 35) — hindered coupling
```

## Are steps one bond at a time? No — the IR is natively multi-bond

Asked 2026-09-07. Verified, all four:

- **One template, two sigma bonds.** Retro-Diels–Alder:
  `C1=CCCCC1 => C=C + C=CC=C`, and on a real adduct
  `O=C1OC(=O)C2C1CC=CC2 => C=CC=C + O=C1C=CC(=O)O1`.
- **Multicomponent.** A Ugi 4-CR is one step with four reactants; all four
  components parse.
- **Cascade / zipper.** A single transformation taking rings 0 → 2 — two rings
  closed in one step.
- **The precis IR already holds it.** `RouteStep.reactants` is a list; a Ugi
  step built in `RouteGraph` renders correctly with four reactants on one line.

**Two real limits, neither of them "linearity":**

1. **`RouteStep.product` is a single string.** One product per step. Fine for
   atom economy (desired product + reactants suffice) but it cannot express a
   step that yields a consequential byproduct or a mixture — which blocks
   E-factor, and blocks saying "this step makes two things". Widening it to a
   list is cheap *now* and expensive after the store is populated.
2. **Planner coverage, not representation.** AiZynth's templates come from
   USPTO, which is dominated by simple one-bond transforms; cascades and MCRs
   are rare in the library, so a planner under-proposes them even though the IR
   holds them fine.

Limit 2 has a strategic consequence worth stating plainly: **multi-bond
disconnections are exactly the high-value ones** — they collapse step count and
`longest_seq`, which is the term that dominates yield on a complex target — and
they are precisely what template planners miss. So the LLM-proposes /
precis-checks loop is not a consolation prize for lacking a planner. **It is
where the wins are.**

## Catalysis: tractable at two levels, not at the third

Asked 2026-09-07. The tractability ladder, stated honestly because the middle
is where all the value is:

| level | tractable? | how |
|---|---|---|
| catalyst as a **condition** on a reaction fact | **yes, today** | `conditions={'catalyst': 'Pd(OAc)2/XPhos', 'loading_mol_pct': 5}` — already in the design, zero new machinery |
| catalyst as an **entity** you aggregate over | **yes, small build** | "what does XPhos give across Buchwald couplings — n, yield range, substrate scope" |
| catalyst **selection by precedent** | **yes, falls out** | reaction-class axis × catalyst entity |
| **mechanistic DFT of a homogeneous cycle** | **no** | see below |
| **enantioselectivity** prediction | **no** | ee turns on ΔΔG‡ of ~1–2 kcal/mol, below reliable DFT accuracy |

**The trap to name: `pathway`/autocatpath does _not_ transfer.** It models
*surface* catalysis — a slab (`slab.element` = a metal), adsorbed intermediates,
NEB on facets. A molecular Pd cycle in solution has no slab, needs solvation and
a conformer search over each transition state, and is a research-grade campaign
per system. Having the `pathway` kind does **not** give catalytic synthesis for
free, and assuming it does would be the expensive mistake here.

**Design consequence: the catalyst must be an entity, not a string in a
conditions blob.** That single change is what makes the tractable level
actually useful — it turns "what catalyst for this transformation on this
substrate class" into a query, and lets the catalyst carry its own sourced
facts (loading, TON/TOF, cost, air-sensitivity) through the same value-table
pattern. A string in a JSONB dict can never be aggregated over.

## Can a reaction fact be a nanopub? Not as it stands — three gaps

Short answer: a reaction fact is an unusually *good* nanopub candidate — it is
already atomic, quantitative and conditioned, unlike the prose claims the
machinery was built for — but it needs three things it does not have.

**First, note the shape mismatch.** This codebase's nanopub assertion is
**sentence-shaped**: `nanopub/aida.py::aida_uri` keys the assertion on a
canonicalised natural-language sentence, and `gates.py` runs
`check_claim_sentence` plus a passage check that must locate the claim
uniquely in the source chunk text. A `reaction_values` row is a *measurement*,
not a sentence. So the row is not itself a nanopub; what is publishable is an
assertion *about* it.

1. **Conditions must be bound into the assertion identity, not sit beside it.**
   "This amide coupling gives 78 % yield" is false by omission. The atomic
   claim is *"(reaction R, under conditions C) gives yield Y."* If the
   conditions live in a sidecar dict while the frozen sentence says "78 %",
   the nanopub asserts something the data does not support — and the AIDA URI,
   keyed on the sentence alone, would collide across genuinely different
   experiments. This is the biggest gap and it is a correctness problem, not a
   completeness one.

2. **Attribution — a transcribed yield is not our claim.** The paper's authors
   made the measurement. Publishing it as *our* assertion is a provenance
   error. The honest form is the one precis already implements: a `finding` hub
   whose supporter is the paper, i.e. "this claim, grounded in that source."
   **So reaction value rows should feed hub construction, not be nanopubs
   themselves.** That also keeps us on the right side of
   `EVIDENCE_SRC_KINDS` — the paper grounds, the reaction ref carries.

3. **An externally resolvable reaction identifier.** Inside precis the
   LinChemIn uid is fine. A nanopub is meant to be dereferenced by strangers,
   and a vendor-internal hash means nothing outside. This is precisely where
   RInChI would have earned its keep, and it does not exist in usable form
   (see the RInChI section). Fallback options: the canonical reaction SMILES
   itself (universally parseable, not compact) or sorted component InChIKeys
   (rdkit-computable, stable, no extra dep). **Pick one before publishing
   anything**, because the identifier is frozen into the nanopub.

Deliberately not proposed here: a *structured* (non-sentence) assertion path.
It may well be the right long-term answer for quantitative claims, but it is a
change to the nanopub core, not to this kind, and it should be argued on its
own.

## Probe-based stability — a different feasibility criterion, not a harder one

Reto, 2026-09-07, on using AFM-like probes to check whether an intermediate is
stable. Two halves, and the usual expectation is inverted:

**The computational half is the tractable one, and the machinery exists.**
"Is this intermediate stable" reduces to: is it a minimum on the PES, and what
is the barrier to its lowest-energy decomposition channel. That is the
`structure` relax ladder (clean → emt → ml → dft), `estimate` for the ms-tier
screen, and `pathway`-style barrier work for the decomposition channel.

**The experimental half is instrumentation we do not have** — but AFM data as
*evidence* already has precedent in the tree: `nm-kind.md` cites AFM-measured
C–C rupture force (~4–6 nN) and runs a min-cut bond analysis against it.

**The real insight is that this is a _different_ criterion, and it is the one
that separates the two make-orders.** In solution you never isolate an
intermediate — it is generated and consumed in situ, so transient instability
is perfectly acceptable. In probe-based assembly you hold one molecule at a
time, so **stability in isolation becomes binding**. A route that is fine in
flask chemistry can be impossible mechanosynthetically, and the reverse.

That maps exactly onto `make-tree-vs-design-tree.md`'s two make-orders (placed
assembly vs bulk synthesis) and yields a concrete rule: **the step DRC is
mode-dependent.** The same step gets different checks depending on which
make-order it sits in — solution mode asks about chemoselectivity and steric
access; probe mode asks whether the intermediate survives isolation, has a
rearrangement barrier above thermal energy at operating temperature, and can be
gripped without reacting with the tip.

This is the same shape as `se-feasibility-and-cost.md`'s central insight —
severity resolved at read time against a capability row rather than hardcoded
at the check site. Here the "capability" is the **assembly mode**. Build the
check registry so mode is a parameter from the start; retrofitting it means
rewriting every check.

## The three ladders — proposed 2026-09-07

Reto's ask: a quick way to tell *can it exist*, then progressively more
effective ways to ask *could it be made like that*, and *are the intermediates
stable*. Rungs below were measured with rdkit only unless noted.

This is the house pattern, not a new one — `structure`'s clean→emt→ml→dft,
`pathway`'s screening→neb→verify, `estimate`'s ms→MLIP→DFT→literature. Three
things make it work:

- **Every rung returns the same verdict shape** — `hard reject` /
  `graded score + coverage` / `insufficient data` — so the cost vector can
  aggregate across ladders without special-casing.
- **You climb only when the cheap rung is ambiguous**, never by default. Same
  rule as `precis-pathway-help`'s "doubt the gate": escalate the *one* step
  whose number is untrustworthy, not the whole route.
- **`insufficient data` is a first-class verdict.** It is not a soft pass.

### Ladder A — can it exist? (a molecule, alone)

| rung | cost | asks | tool | verdict |
|---|---|---|---|---|
| **A0** parse + valence | µs | is the graph chemically legal | rdkit sanitize | **hard reject** |
| **A1** 3D embed | 0.1–1 s | is the connectivity geometrically realisable | ETKDGv3 | **hard reject** on embed failure |
| **A1b** strain | +MMFF | how strained, per heavy atom | MMFF94 | graded |
| **A2** structural alerts | ms | known-problematic motifs | `FilterCatalog` | **advisory only** |
| **A3** semi-empirical | ~1 s | does it stay put; HOMO–LUMO gap | tblite GFN2-xTB | graded |
| **A4** DFT | hours | the real answer | compute lane | reserved |

Measured:

```
A0   C(C)(C)(C)(C)C  REJECTED (valence)   CN(C)(C)(C)C  REJECTED (5-bond N)
A1   anti-Bredt bicyclic  ->  NO 3D EMBEDDING (geometrically impossible)
A1b  cyclohexane 0.4 · benzene 2.7 · cubane 8.6 · tetrahedrane 14.9  kcal/heavy atom
A2   765 alert entries loaded (PAINS + BRENK + NIH)
```

**A0 is the highest-value-per-microsecond check in the whole design**, because
invalid SMILES is the single most common failure mode of an LLM-proposed
molecule. Nothing downstream should ever run on an unsanitised graph.

**A1b calibrates against knowns, and it works:** cubane at 8.6 is strained but
genuinely isolable; tetrahedrane at 14.9 is not (only persubstituted
derivatives have ever been made). So a per-heavy-atom strain threshold sits
somewhere between them — **set it from that pair and other knowns, never
invent it.**

**A2 must never gate — proven by its own output: aspirin trips
`phenol_ester`.** PAINS/Brenk/NIH are medicinal-chemistry *nuisance* filters,
which overlaps with but is not the same question as "can this exist". Same
discipline as SAscore: it ranks, it never rejects.

Two honest caveats. A0 does **not** catch all nonsense — `c1ccc1` (a
four-membered "aromatic" ring) parses. And A3's `tblite` ships no
linux-aarch64 wheel (the existing `[estimate]` platform marker), so the
semi-empirical rung is unavailable on the aarch64 cluster nodes — it runs on
the Mac and x86_64 only.

### Ladder B — could it be synthesised like that? (a proposed route)

Outranking every rung: **stock termination is the hard gate.** A branch ending
in a non-purchasable leaf is not a worse route.

| rung | cost | asks | tool |
|---|---|---|---|
| **B0** parse + balance | µs | do steps parse, are atoms conserved, does it reach the target | rdkit |
| **B1** precedent | ms | does a known template give this disconnection; class precedent `n` + yield distribution | template match + reaction store |
| **B2** step DRC | ms–s | competing sites (chemoselectivity); `%V_bur` (steric) | rdkit |
| **B3** compatibility | ms | does any condition destroy a group elsewhere | FG matrix (**to author**) |
| **B4** planner | min, container | does AiZynth independently find this, or better | AiZynth |
| **B5** mechanism | hrs–days | is the unprecedented step real | estimate / pathway / DFT |

B0–B2 are all in-process and already demonstrated. **B1 is what produces the
risk map** — the per-step precedent line that says which bond nobody has
formed. B4 is corroboration, not authority: a planner failing to find your
route is weak evidence against it, given its template coverage.

### Ladder C — are the intermediates stable?

**Ladder C is Ladder A run over every node of the route.** That is the whole
design; no third mechanism is needed. An intermediate is a molecule, and "can
it exist" is the same question.

What changes is **the threshold, set by assembly mode** (per the mode-dependent
DRC above):

- **solution mode** — the intermediate need only persist long enough to react
  onward under the conditions of its own step. Transient instability is
  routine and is not a defect.
- **probe mode** — the intermediate is held in isolation, so it must survive
  alone: a rearrangement barrier comfortably above thermal energy at operating
  temperature, and no reaction with the tip.

Same computation, different pass mark. That is exactly
`se-feasibility-and-cost.md`'s "severity resolved at read time against a
capability row", with assembly mode as the capability — which is why mode must
be a parameter of the check registry from the first commit.

### What this buys, in one line

`can_exist(target)` in a millisecond, `can_exist` over every intermediate in a
second, a precedent-and-DRC risk map over the route in seconds, and a planner
or DFT escalation only where the cheap rungs came back ambiguous.

## Wiring to what already shipped

A `make` step and a `route` step both gain an optional `reaction=rx<id>` in
step meta. The reaction store then becomes the shared substrate under both the
engine-planned route and the human-authored make-tree, instead of each
re-typing conditions into its own free-form meta. That is the "two trees over
shared leaves" rule from `make-tree-vs-design-tree.md`, applied one level down:
two *process* representations over shared reaction facts.

This is also what makes the whole thing serve Reto's stated purpose. A
dreamed-up structure gets a make-tree; its steps point at reactions that carry
real, sourced, cited yields; the cost vector says how much it hurts and the
stock-termination DRC says whether it is a thing at all.

## Status — rung 1 BUILT 2026-09-07

Decisions taken (all previously recommended in this doc, unopposed):

- **kind is `rxn`, handle `rx`** — `reaction` collides with the pathway graph's
  edge-kind value (see the naming section).
- **core kind**, not a `precis_chem` plugin — it is a sourced-fact store with
  no chemistry on the request path, and the plugin kinds are exactly the ones
  that ended up uncitable.
- **both identity axes**, `uid_strict` + `uid_transform`, plus
  `reaction_class` holding an RXNO id.

Landed: migration `0157_rxn_kind.sql` (kind row, `rxn_properties`,
`rxn_values`, three meta indexes, 10 seeded core properties) ·
`store/_rxn_ops.py` (`RxnMixin`, wired into `Store`) ·
`handlers/_rxn_ids.py` (canonicalisation + both keys, rdkit lazy) ·
`handlers/rxn.py` (`RxnHandler`) · `rx` in `handle_registry` ·
`RxnHandler` in `dispatch.boot` · `rxn_smiles=` / `reaction_class=` /
`source_licence=` on the `put` verb and `reaction_class=` on `search` in
`tools/core.py` · `precis-rxn-help` + `precis-overview` + `precis-toolpath`
entries · `tests/test_rxn_ids.py`.

Notable implementation choices beyond the design above:

- **`method=` and `source_licence=` are wired end-to-end** on this kind from
  the first commit. `method` is wider than material's vocabulary
  (`patent-extracted`, `literature-reported`, `predicted`, `computed`) because
  this store ingests bulk data and a curated yield must stay distinguishable
  from a mined one. `source_licence` travels *with the number* so an NC-licensed
  price cannot leak into a nanopub by forgetting.
- **rdkit is lazy and optional.** Without the `[chem]` extra the SMILES is
  stored verbatim, identity keys are **not** invented, and both the write
  response and the read page say so.
- **Writing a reaction whose `uid_transform` already exists prints a pointer to
  the sibling** rather than silently creating a near-duplicate.

Correction to an earlier claim in this doc: **gripe 329157 was wrong and has
been retracted.** `method=` *is* already wired end-to-end for material and
component — the false report came from reading an `rtk`-digested grep as if it
were a complete match set. The lesson generalises: a grep whose *absence of
hits* is load-bearing must be re-run without `rtk`.

## Ship order

Each rung is independently useful and independently shippable.

1. **`reaction` kind** — entity + property registry + value table + `rx` handle
   code + skill. Manual `put` only, **no scoring, no import**. This alone
   closes the actual gap and satisfies the "extendable from papers with cites"
   requirement. **Must carry both axes from the start** — `uid` (exact
   identity) *and* `reaction_class` (precedent transfer); retrofitting a class
   axis onto a populated store is far more expensive than declaring it now,
   and without it the store cannot serve a novel target at all.
2. **USPTO CC0 seed** — ADR 0053 seam, canonical-uid collapse key,
   `provenance='external'`, `method='patent-extracted'`. Note the shape
   constraint found above: because `rxnutils` pins `numpy<2`/`rdkit<2024`/
   py<3.13, this is a **container or one-shot offline ETL emitting plain
   rows**, not an in-process adapter. The ADR 0053 rule that adapters are pure
   and DB-free is what makes that split clean.
3. **SAscore + atom economy** — pure functions over rdkit, **no new deps**
   (both measured working), attached via `analyzed-by` with staleness.
4. **In-process route descriptors** — a `[synth]` extra (linchemin +
   packaging), lazily imported, scoring a route or a lowered `make`-tree from
   bare reaction SMILES. Fills `RouteGraph.metrics` for stub and hand-authored
   routes, which has been unreachable since slice 2. Independent of rung 2 —
   pull it earlier if dreamed-up syntheses matter more than the USPTO seed.
5. **Route cost vector + stock-termination DRC + the "N of M grounded"
   coverage line** — the first rung that *consumes* those metrics.
6. **`make`/`route` step → `reaction` linkage.**

## Deferred, named so they are not re-derived

ORD import (after USPTO proves the seam); RAscore and SCScore (need trained
models, and both inherit their source planner's biases); PMI and E-factor
(need solvent and workup volumes — recursive through yields, not summed);
yield *prediction* (as opposed to yield recording); ChemXtract-style NER +
event extraction of yields and conditions from our own paper corpus (the
report's named best practices — per-field confidence scoring, unit
normalisation, schema validation, human-in-the-loop — are the acceptance
criteria when it arrives); biocatalytic routes via Rhea/RetroBioCat; a
monetised scalar objective as a default; RB-SAScore's fragment-level split.

### Packages surveyed and rejected (2026-09-06) — do not re-investigate

- **syba** — not on PyPI; **GPL-3.0**, which is likely disqualifying on its
  own. Abandoned since 2021, and `pip install git+…` is *broken*: its
  resources are git-LFS pointers, so it fails with `gzip.BadGzipFile`. ⚠️ The
  PyPI package named `SyBA` is an unrelated bacterial-gene tool — a live
  mis-install trap.
- **RAscore** — not on PyPI; abandoned 2021, hard-pinned to Python 3.7 +
  `tensorflow-gpu==2.5.0`. Effectively a museum piece; if the signal is wanted,
  run aizynthfinder directly. ⚠️ PyPI `rascore` is an unrelated RAS-protein
  package — another mis-install trap.
- **scscore** — not on PyPI, but MIT and genuinely vendorable:
  `standalone_model_numpy.py` needs only numpy + rdkit. Costs one `np.bool` →
  `bool` patch (removed in numpy ≥ 1.24) and one model dir out of a 410 MB
  repo. This is the one deferred score with a cheap path if we want it.
- **rxnmapper** — atom mapping, but torch + transformers, and `import
  rxnmapper` breaks on Python ≥ 3.12 without `setuptools<81` (`pkg_resources`).
  Container-only.
- **rxn4chemistry** — tiny, but a network client for IBM's cloud needing an API
  key, and it pins `beautifulsoup4==4.9.0` exactly. If we ever want it, vendor
  the two calls rather than depend on it.
- **orderly** (ORD → tabular) — pins `ord-schema <0.4` and `rdkit <2023`,
  irreconcilable with current ord-schema 0.8.x. Dead end.
- **Stoichiometry balancing** — not needed for atom economy (mass ratios are
  rdkit arithmetic). If real coefficient solving is ever wanted, `chempy`
  (BSD-2) does it but drags in pyodesys/pulp/matplotlib; `chemformula` (MIT,
  one dep) is the light formula-only option. ⚠️ Avoid `atomecon` — first
  published 2026-09-06, no track record.
- **aizynthfinder** stays containerized permanently, and now for a concrete
  reason: 4.4.1 pins `numpy<2.0.0`, `rdkit<2024.0.0` and caps at Python
  `<3.13`. Those pins alone would wreck a shared venv — the existing
  `python:3.11-slim` image is not conservatism, it is necessity. (Worth noting
  its base install is `onnxruntime`, not tensorflow; and 4.4.1 exists against
  our pinned 4.3.2.)

## Readiness — checks done 2026-09-07

Cleared, so they do not bite on day one:

- **`rx` is free** as a handle code (65 core codes checked; plugin codes are
  only `es`/`nm`/`se`). **`reaction` is free** as a kind slug.
- **No sibling worktree is touching chem** — no in-flight overlap.

### The byproduct ambiguity — decide before the import, not after

Measured earlier: `CCBr.N#C[Na]>>CCC#N` and
`CCBr.N#C[Na]>>CCC#N.[Na]Br` produce **different uids**. Chemically correct —
they are different balanced equations — but operationally awkward, because the
literature records byproducts inconsistently. The same transformation will
fragment into two or more refs depending on whether a given source bothered to
write the salt or the water.

That directly undermines the collapse key: a uid-keyed store will *not*
converge USPTO and ORD rows for the same chemistry. Options, none free:

- **normalise** — strip a curated byproduct set (water, HX, salts) before
  hashing. Cheap, and wrong at the edges (water is the product in a hydrolysis).
- **key on (reactants, desired product)** and keep the full balanced equation
  as an attribute. Loses balance information from the identity but matches how
  chemists actually refer to a reaction.
- **key on both** — a strict uid plus a loose "transformation" uid, and dedup
  on the loose one. Most work, most faithful.

**This is cheap to decide now and expensive after the store is populated**,
because it is the primary key of the fact table. My lean is the third option:
the strict uid is already computed for free, and the loose key is what every
query will actually want.

## Naming — rename the kind to `rxn` (sweep, 2026-09-07)

A full collision sweep cleared every *structural* choice: `rx` is free across
all 72 core + 4 plugin handle codes; `reaction_properties`/`reaction_values`
collide with no table; `0157` is free (plugin migrations live in per-plugin
namespaces restarting at 0001, so they cannot conflict); `rxn_smiles` and
`reaction_class` appear nowhere in `src/`; no `PRECIS_REACT*` setting exists;
`catalyses`, `reaction-of` and `precedent-for` are all free against the 70
existing relation slugs.

**But the name `reaction` itself fails, on two counts.**

1. **`"reaction"` is already a value of a field literally named `kind`.** In the
   pathway graph edge model it is the *default edge type*:
   `precis_web/routes/refs.py:1005` — `"kind": e.get("kind") or "reaction"`
   (contrasted with `"supply"` at `:1246`), and `precis_pathway/toon_views.py:255`
   emits it into a TOON table that has a literal `kind` column. **An agent
   reading `kind: reaction` in pathway output and calling
   `get(kind='reaction', …)` would get a real, wrong, silently-successful
   answer.** That is the exact failure class this codebase refuses everywhere
   else.
2. **The repo already fought this word once.** `precis-lab-help.md:27` and
   `precis-toolpath-help.md:154` both write "reaction-*network*" with
   deliberate italics on *network* — disambiguation prose written to keep
   `pathway` (catalyst surface network) apart from `route` (retrosynthesis).
   Adding a third kind named `reaction` makes it three-way. Nine skills already
   use the word; `precis-pathway-help` has it in its **title, summary and two
   keyword questions**, so a new `precis-reaction-help` would compete directly
   with it in skill search with no obvious ranking.

**Recommendation: name the kind `rxn`, keep the handle code `rx`.** It is free
everywhere (its only occurrence in `src/` is a loop variable in
`precis_chem/aizynth.py`), it carries zero prose collision, and chemists read
it instantly. Other free options if `rxn` reads too terse: `transformation`
(the standard med-chem term for exactly this) or `prep`. Avoid `synthesis` —
this doc's own thesis is that a monolithic "synthesis" kind would duplicate
`route` + `make`, and the name re-invites that misread.

### Two follow-ons the sweep surfaced

- **`method=` has two incompatible senses.** `material_values.method` and
  `component_spec_values.method` are scalar provenance enums
  (`measured|datasheet|estimated`), which is what we want — but
  `struct_runs.method` (migration 0084) is a **jsonb DFT fingerprint**
  (`functional`, `cutoff_eV`, `kmesh`), consumed by
  `structure.py::_format_method_fingerprint`. Wiring a single `method=` onto
  the shared `put` verb would make one kwarg mean `str` for three kinds and
  `dict` for `structure`. Resolve before doing the gripe-329157 wiring —
  either a distinct kwarg name for the provenance sense, or per-kind coercion.
- **`planner_prompt.py:1004 _CITE_HANDLE_RE` is an allowlist**
  (`pc|pk|qc|dk|pa|pt|cf|da|fi`), not a generic two-letter pattern. A new
  handle is **invisible to the planner's cite scan** until added. Every other
  handle regex in the tree is generic and needs no change.

## Raising issues to the designing LLM early

The channel already has a design-of-record — `attached-models-layer.md`'s "a
way for code to flag issues to the ai" — and three tiers, two of which exist.
Applied here:

**Tier 1 — propose-time, synchronous. The earliest and most valuable.**
`pathway` already ships the pattern: `mode='preview'` builds the network and
returns it with **no compute spent**, so the agent argues with a bad plan
before paying for it. Mirror it exactly:
`put(kind='rxn'|'route', …, mode='preview')` runs Ladder A0–A1 and B0–B2 and
returns the verdict **without minting anything**.

This also fixes a live defect: `route.py::put` currently `insert_ref`s *before*
discovering there is no route node, so a failed call still leaves a ref. **The
check must run before the write.**

**Tier 2 — read-time.** The risk map rides on every `get`, not on request. An
agent that reads a route always sees which step is unprecedented; there is no
"remembered to ask" failure mode.

**Tier 3 — async.** When a new value row contradicts a cited number, or an
upstream structure changes, code files a gripe/alert on the design. That is the
existing `analysis-stale` lane plus the gripe machinery, not a new subsystem.

**What makes a finding _actionable_ is the house `next=` convention.** Every
`BadInput` in this codebase carries `next=` with the exact corrected call. DRC
findings must do the same — not "2 competing amine sites" but that plus the
remedy:

```
step 3  ⚠ 2 competing amine sites (expected 1)
        next: protect the aniline (Boc), or use the acyl imidazole at 0 °C
```

A verdict an agent cannot act on is a log line, not a channel.

**And the meta-channel: mine the confusions.** Systematic agent errors show up
in prod job transcripts as `[error:*]` tool-call failures; harvesting those into
skill and error-message fixes is how the *system's* designers learn what the
prose got wrong. A kind this novel should expect a first-month crop.

## Stock and price — the two ungrounded inputs

Both were hand-waved above and both are load-bearing: **stock termination is the
hard gate**, and `c_i` in `CostScore = Σᵢ cᵢ / ∏_{j≥i} yⱼ` is the money term.
Source selection is out for research; the *shape* is decidable now.

### Price has the same shape as yield — and the schema already anticipated it

**A price is not a property of a molecule.** It is a property of
`(compound, vendor, pack size, date)`. A single `price` column would be exactly
the lie the multi-row design exists to prevent — the same mistake as collapsing
twelve reported yields into an average.

So price is a **sourced value row like any other**: `value_num` + `conditions`
carrying vendor and pack size + `as_of`. This is not a new mechanism, and the
schema was built for it — `material_values.as_of` is commented in migration
0092 as *"load-bearing for cost_per_mass etc."*. The precedent is explicit.

Three consequences worth stating before anyone writes the column:

1. **Only leaves carry prices.** A novel intermediate has no price by
   definition; its cost is *derived* through the yield chain. So the money term
   sums over stock leaves only, and the coverage line is `component`'s BOM line
   verbatim: **`priced: 3 of 5 leaves`**.
2. **A price without a quantity is not a price.** 100 mg at catalogue rate and
   1 kg at bulk rate are different businesses, and a route optimised for one can
   be wrong for the other. **The cost vector must state the scale it assumes**,
   or it is silently comparing incomparable routes. Pack-size tiers, where a
   source gives them, are the honest representation — a curve, not a number.
3. **Redistributability matters more than access.** A price licensed for
   internal use only cannot appear in a nanopub or a citable claim. That is the
   same segregation discipline already recorded for Reaxys/Pistachio, and it
   should be a `maturity`/licence facet on the row rather than a footnote —
   otherwise a restricted number leaks into a published artefact by accident.

### Licensing posture (Reto, 2026-09-07): free and open first

DIY/hobby scale, possibly university affiliation; **stay free and open where
possible, but know the size of the gap.** That makes source selection a
deliberate trade rather than a default, and it ranks the needs:

- **Reaction corpus — the free option is genuinely good.** USPTO is CC0 and
  large. ORD is open but **CC-BY-SA**, and share-alike is the thing to think
  about *before* ingesting: it is compatible with staying open, but it
  constrains derived work, and it is a poor mix with any restricted source in
  the same table. Segregate by licence at the row level, not by good intentions.
- **Structures — free is fine.** PubChem is public domain; COD is open.
- **Reagent price — expected to be the hard gap**, since catalogue pricing is
  exactly what vendors monetise.
- **Curated conditions — the other expected gap.** The Reaxys/Pistachio
  value-add is human-curated conditions and yields at a coverage USPTO's
  text-mined extraction does not match. We are trading *curation quality*, not
  raw volume.

Recording this as a decision means a later contributor cannot quietly "fix" a
coverage complaint by pulling in a restricted source.

### Price sources — surveyed 2026-09-07

Free price data **does** exist (an earlier draft of this doc said otherwise).
It is stale, coarse and mostly NC-licensed. Ranked for a free-**and-open**
posture, where the distinction between "free" and "open" decides the ranking:

| source | size | licence | verdict |
|---|---|---|---|
| **ChemCost** (HF `nips2026-chemcost/chemcost-bench`) | 1,793 SMILES | **CC BY 4.0** | **the only genuinely OPEN one.** Small, but the only source with real **pack tiers** — `{min, median, p75, max, n_quotes, n_suppliers}` + `procurement_pack_g`. Anonymous review release, may move. |
| **ASKCOS `buyables.json.gz`** | **280,469** records | **CC BY-NC-SA 4.0** | best coverage. `{smiles, ppg, source}`, USD/gram, **whole dollars, capped at $100**, 2017-vintage. LabNetwork 156,990 · eMolecules 103,189 · Sigma 20,290. |
| **CoPriNet** test set | 99,957 | repo MIT, **data not** | **avoid.** Derived from a 2021 Mcule snapshot whose ToS forbids redistribution — chain of title is not clean. |
| **ZINC tranche tier letter** | all of ZINC | free, shareable per-molecule | the honest **availability gate** — not a price. See below. |

**The NC problem is the decision.** `CC BY-NC-SA` is free-as-in-beer but is
**not open**. Building the cost axis on the ASKCOS table means the derived
artefact inherits NC + share-alike, and its provenance is uncomfortable
besides — ASKCOS's own docs say the data was *"either extracted from the
website or through third party collaboration"*, so the relicensing rests on
rights they may not have held. Fine for a private hobby project; **do not put
those numbers in a nanopub or any published artefact.**

**ZINC's purchasability tier vindicates the fallback proposed above.** It is
the **4th letter of the tranche filename** — `A`,`B` in stock · `C` via agent ·
`D` make-on-demand · `E` boutique · `F` not for sale — free, current, and
explicitly shareable per molecule. (CartBlanche deliberately zeroes prices:
`vendor['price'] = 0  # ... encourage users to contact vendors`.) That is a
coarse availability tier exactly as sketched, and for the open posture it
should be the **primary** stock gate, with prices an optional overlay.

Enamine REAL: the tier *structure* is citable ("all REAL compounds have flat
prices"; m-REAL ~50 % over s-REAL) but **no absolute figure is published**. The
best citable anchor is the ZINC20 paper, Box 1: on-demand compounds "often
near $100, generally less than $200 each."

### Do not reinvent the cost scorer

`CostScore = Σᵢ cᵢ / ∏_{j≥i} yⱼ` as derived above is **already implemented** —
AiZynthFinder's `RouteCostScorer`, documented as "From Badowski et al. *Chem
Sci.* 2019". Cite that rather than presenting it as ours. Price plumbing is
`StockQueryMixin.price()`/`amount()`; only 2 of ~19 scorers use price
(`PriceSumScorer`, `RouteCostScorer`) and **neither is auto-loaded**.

⚠️ **Fail-open trap to NOT copy.** AiZynth's stock check does
`except StockException: return True` — an unpriced or erroring stock query
**silently passes** the price stop-criterion. Our stock termination is a *hard
gate*; it must **fail closed**, and an unresolvable stock lookup must render
`insufficient data`, never a pass.

**SPARROW** (Coley group, **MIT**, *Nat. Comput. Sci.* 2024) is the only
genuinely cost-aware open planner, and its `Coster` abstraction —
`NaiveCoster` / `ChemSpaceCoster` / **`LookupCoster`** / `QuantityLookupCoster`
— is the ready-made seam: `LookupCoster` takes a SMILES→cost CSV, so the
ASKCOS table drops straight in. It bundles **no** price data itself, which is
the shape of this entire field: **the algorithms are open, the data is your
problem.**

### Vendor-API notes

Live vendor pricing should be an **optional user-supplied key** (SPARROW's
posture) — that keeps this project redistributable while letting a keyed user
pull current numbers *they* must not republish. Also: **Alfa Aesar no longer
exists** (folded into Thermo Scientific, Oct 2021); TCI's terms contain no
anti-scraping clause while Sigma and Fisher ban it explicitly; and ⚠️ the
`apis.io` "Sigma-Aldrich Pricing and Availability API" listings are
**auto-generated phantoms** — no endpoints, no archive of the cited docs. Do
not build against them.

**Do not synthesise a number.** A route costed from invented prices is worse
than an uncosted one, and the `—` render plus the coverage bracket already
exist to say so.

## Hazard data — the `risk` axis is groundable for free (verified 2026-09-07)

The cost vector's `risk` term was ungrounded. It no longer needs to be, and the
free path is clean — but the licensing has a trap that a naive read gets
backwards.

### The stale-licence trap

**Do not take ECHA's terms from PubChem's `LicenseNote` field — it is stale**
and says non-commercial-only. The current ECHA legal notice (§5.1.1 A) allows
reuse "for commercial and non-commercial purposes" given attribution
(`Source: European Chemicals Agency, http://echa.europa.eu/`) and unaltered
integrity.

**Commerciality is not the blocker. Bulk replication is.** Two exceptions bite:
replication "in whole or in substantial part, of the ECHA databases" needs prior
written permission, and "systematic automated data collection activities
(including scraping, data mining, and extraction and re-utilisation)" of a
substantial part are prohibited — *except* that "research organisations" as
defined in Art. 2 of Directive (EU) 2019/790 may perform TDM for scientific
research under Art. 3.

**This is where "maybe university" stops being incidental.** The TDM carve-out
is scoped to research organisations; a hobbyist is not one. Affiliation is the
difference between a legal route and a prohibited one for bulk ECHA use.

*Verification caveat:* `echa.europa.eu` returns 403 to scripted clients, so the
notice was read from a Wayback snapshot (2025-08-28). Re-check in a browser
before relying on it commercially.

### The clean free sources, in preference order

1. **EUR-Lex for CLP Annex VI.** The same harmonised-classification legal text,
   from a publisher whose notice explicitly permits commercial and
   non-commercial reuse: editorial content **CC-BY-4.0**, metadata **CC0**.
   This routes around the ECHA reproduction restriction entirely by taking the
   content from the source with the permissive licence. **Preferred.**
2. **PubChem `CID-LCSS.xml.gz`** —
   `ftp.ncbi.nlm.nih.gov/pubchem/Compound/Extras/CID-LCSS.xml.gz`, ~477 MB,
   `.md5` sidecar, refreshed 2026-09-06. One bulk file instead of paging an
   annotations endpoint. Roughly **26,000 CIDs**; sampled records carry CAS plus
   a GHS Classification section, often Primary Hazards, Flammability/
   Explosivity, Carcinogen Classification, PPE.
3. **PubChem GHS annotations, pages 1–5 only** — that slice is the
   **CC-BY-4.0** CLP Annex VI content (~4,078 records, 2,573 CIDs, 73 H-codes).
   Pages ~5–340 are ECHA C&L *notifications* and the tail is NITE-CMC (Japan),
   with different terms — so the page range is a licence boundary, not a
   pagination detail.

Also available if a CAS-keyed table is wanted: the ECHA Annex VI ATP22 xlsx
(4,425 rows, applies 2026-05-01) — but **its own disclaimer forbids commercial
use and further reproduction**, so prefer EUR-Lex for the same content.

### API gotcha

`"Laboratory Chemical Safety Summary"` is **not** a valid PUG-View heading
(returns `PUGVIEW.BadRequest`). The valid heading is **`GHS Classification`**
(`Type=Compound`). Full heading list (724):
`pubchem.ncbi.nlm.nih.gov/rest/pug/annotations/headings/JSON`.

### What this buys

H-codes per compound, CAS-keyed, free and redistributable. Enough to score a
route's `risk` term on reagent hazard — and enough to make the green-chemistry
angle real rather than aspirational. **Gap closed**, subject to the
browser-recheck caveat.

## The free/open stack — surveyed 2026-09-07

**The headline correction: the gap is far smaller than this doc predicted.** It
guessed that curated *conditions* and *yields* would be the unavoidable
paid-only gaps. Both are wrong.

### The hard gate is currently terminating against a 2020 snapshot

`download_public_data` fetches figshare `23086469` =
`zinc_stock_17_04_20.hdf5`, 663 MB, **17,422,831 rows and exactly one column:
`inchi_key`** — a ZINC snapshot **frozen 17 April 2020**. No SMILES, no vendor,
no price. Licence MIT, so it *is* the one clean redistributable default.

**Replace it with Mcule In Stock: 7,174,166 compounds, 105 MB `.smi.gz`,
updated 2026-08-02, fully anonymous public bulk API**
(`mcule.com/api/v1/database-files/`, 67 files, all `"public": true`). Mcule's
ToS permits "the Client's own business purposes only" — usable, **not
redistributable**, so keep the MIT ZINC file as the shippable fallback.

Ruled out: **Enamine REAL** bars "incorporating any Enamine Data into any
computational system, including… traditional cheminformatics tools", and
registration demands a `Company`. ZINC bulk redistribution needs written
permission (per-molecule results are fine, and the tier letter is free).

### Yields: paying buys essentially nothing

The well-controlled yield data is HTE, and **all significant HTE is free**:
Pfizer **HiTEA 39,000+** conditions (MIT), USPTO curated yields **~500,000**
(MIT), Buchwald–Hartwig 3,955 and Suzuki–Miyaura 5,760 (MIT), ORD ultraHTE
50,688, RGD1-CHNO **176,992** DFT reactions with transition states (CC BY 4.0).
Reaxys yields are heterogeneous literature-reported numbers — *less*
controlled, not more. ⚠️ CJHIF is **withdrawn** for copyright; do not plan on it.

### Conditions: QUARC transfers the paid-data value

`quarc-oss` predicts agents, **temperature, and reactant/agent equivalence
ratios**, **MIT for code *and* weights** — and those weights were **trained on
Pistachio**. That obtains Pistachio-derived condition knowledge without a
Pistachio licence, on one CPU box. The corpus gap (Reaxys 69 M with conditions ·
Pistachio 13.1 M · ORD 4.86 M · USPTO 1.8 M) stays 15–30×, but the *model* gap
largely closes. You cannot retrain without Pistachio-format data; ORD substitutes.

### `reaction_class` — the open question, answered

**Store RXNO ids as the canonical class regardless of which classifier runs.**
RXNO is **CC BY 4.0**, 686 classes, OWL/OBO (no JSON-LD — that PURL 404s), and
is **a dictionary, not a reader**: no SMARTS, no SMILES→class mapping, frozen
since 2021-12-16. NameRxn *emits* RXNO ids; the proprietary part is recognition.
Storing RXNO ids keeps us interoperable with NameRxn-derived data forever, and
attribution is the whole obligation.

Recognisers, both offline: **Rxn-INSIGHT** (pip, MIT, Ghent; 91.1 % classify /
95.5 % name on 50 k; needs rxnmapper ⇒ torch) and **`reactionclassifier`**
(pip, MIT, Schwaller/EPFL; **14,060-entry 5-tier RXNO-seeded** taxonomy in the
wheel; neuro-symbolic — a template must reproduce the product before it labels;
**very new, 2026-06, unproven**). **The real gap is here**: NameRxn ~89.3 %
coverage vs 64–68 % free — **20–25 points of recall**.

### Compute — three install traps and one aarch64 reprieve

- ⚠️ **`pip install crest` installs the WRONG package** (an unrelated REST CLI).
  Grimme's CREST is **conda-forge only**.
- ⚠️ **Psi4 is not on PyPI** (404). Conda-forge only.
- ⚠️ Use **`tblite`**, not `xtb-python` — the latter has had no release since
  2022 and is linux-x86_64 only.
- ✅ **PySCF is the QC pick**: `pip install pyscf`, **Apache-2.0**, wheels for
  macOS arm64 **and linux aarch64**, deps only numpy/scipy/h5py. That covers the
  aarch64 cluster nodes where `tblite` cannot go.
- **CREST is too slow for routine use** — a 747-molecule benchmark reports
  ~20–27 min/molecule. Default to ETKDGv3 + tblite reoptimisation (as the
  ladder already proposes); reserve CREST for cases that matter.
- ⚠️ **MACE-OFF licence trap**: clause 13(a) requires work "undertaken at an
  educational, non-profit, charitable or governmental institution" — *narrower*
  than non-commercial, and an unaffiliated hobbyist is arguably outside the
  grant. Use **AIMNet2** or **Egret-1** (both MIT).
- LGPL nuance: obligations attach to *distribution*, not use, and shelling out
  to a CLI binary is a materially weaker trigger than linking. CREST/xtb/Psi4/
  tblite impose nothing on a private system.

### Also worth having

**COD** — **CC0**, **534,912** entries, rsync/REST: the only unrestricted
"has anything like this been made" oracle (CSD is commercial; its free tiers
are not a precedent index). **SureChEMBL** bulk, **CC BY 4.0**, ~17 GB parquet:
patent-scale precedent without Reaxys. **AI4Green** ships
**`hazard_codes.db`** — 16 KB SQLite, 114 rows banding H-codes into VH/H
severity — which *is* the H-code→severity rubric the `risk` axis needs, plus
`CHEM21_full.csv` (53 solvents, S/H/E scores). Code AGPL-3.0; the CHEM21
numbers remain CC BY-NC 3.0 — attribute both. **RetroRules v3** 1,174,216
templates and **EnzymeMap** (MIT) for the biocatalytic direction. ASKCOS v2
exposes **25 Dockerised MIT microservices** (`context_recommender`,
`reaction_classification`, `scscore`, `pathway_ranker`, …).

### Copyleft / licence landmines to track

`ord-data` **CC-BY-SA** (propagates into any published derived DB) · ChEMBL
CC BY-SA 3.0 · SYBA GPL-3.0 · RetroBioCat-2 and ASKCOS's template-relevance
model CC-BY-NC · **TextReact has no LICENSE file at all** (all rights reserved
— do not vendor) · MACE-OFF institution-only · KEGG bulk needs an institutional
subscription.

## Open questions for Reto

- **Plugin or core kind?** `route` is a dark `precis_chem` plugin and is
  consequently uncitable, un-taggable, `corpus_role="none"`. The reaction
  store's entire purpose is citation. My lean is **core**, like `material` and
  `component` — it is a sourced-fact store with no chemistry on the request
  path (uids and SMILES are strings; rdkit and linchemin are needed only by
  the scorers, and both import lazily) —
  leaving the *engines* in `precis_chem`. The cost: it splits the chem surface
  across core and plugin, which two backlog items warn against.
- **Where does the cost vector live** — a `view='cost'` on `route`, or an
  attached `estimate`/`finding` per the attached-models layer? Lean: attached,
  because that is the only path that gets staleness for free.
- **Does a reaction need to ground evidence directly** (i.e. add `reaction` to
  `EVIDENCE_SRC_KINDS`), or is "claims cite the paper chunk, the reaction ref
  carries the value rows" sufficient? Lean: sufficient — it sidesteps the
  computed-artefact blocker entirely and keeps one citation ladder.
- **Is `yield` one property or two?** Isolated vs assay/NMR yield are
  different numbers routinely reported side by side. Lean: one property with
  the distinction in `conditions`, matching how `material` handles measurement
  method.

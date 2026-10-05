---
id: precis-taxon-help
title: precis — taxon (term taxonomy nodes)
summary: put and read a taxon — a named term with a definition, an earned status and an optional dimension
answers:
  - how do I add a term to the taxonomy?
  - which meta keys can a taxon carry?
  - why is my taxon refused with status='systematic'?
  - how do I give a taxon an SI dimension?
  - how do I put a taxon under a parent, and what is an axis?
  - why is my taxon refused under a parent (cycle, missing key, wrong kind)?
  - how do I see the chain from a taxon up to its root?
  - how do I list the narrower terms under a node, on one axis?
  - why is my put refused as a duplicate, and how do I mint anyway?
  - how do I address a taxon by name path like measurand/temperature?
applies-to: get/search/put/delete/tag/link (kind='taxon')
status: active
tags: [workflow]
kinds: [taxon]
---

# precis-taxon-help — taxon

A `taxon` is a named term: a name, a one-sentence definition that states the
whole meaning on its own, and optional structure (dimension, aliases, a
required-key contract on root nodes). Handle `tn<id>`. Nodes form a hierarchy
(a node can have several parents). Which node to attach a ref to, when to
mint, and how to keep siblings from overlapping: `precis-classify-help`.

## Add a term

```python
put(kind="taxon",
    text="turnover frequency — the number of catalytic cycles per active site per unit time",
    meta={"dimension_kind": "si", "si_vector": "0,0,-1,0,0,0,0",
          "canonical_unit": "1/s", "aliases": ["TOF"]})
```

`text` is `<name> — <definition>` (em-dash, en-dash or newline between). The
definition becomes the searchable card, so write it to be matched by meaning,
not by the name.

A put that looks like an existing node is refused, naming it (`tn12 turnover
frequency (rate/turnover-frequency)`) with the reason. Candidates are: same
name, alias or slug as the new node's name or aliases; then, when the
embedder is up, a node whose card is within cosine distance 0.25 (lexical
only when it is down). A candidate with a different dimension is never
offered: both nodes carry `dimension_kind` and the kinds differ, or both are
`si` with different `si_vector`. A node with no `dimension_kind` is never
ruled out this way. To mint anyway: `put(..., dedup=False)`.

## meta keys

Only these are accepted; any other key is refused and the allowed set is listed.

| key | value |
|---|---|
| `aliases` | list of strings |
| `dimension_kind` | `si`, `currency`, `count`, `dimensionless`, `scale` or `categorical`; omit if unknown |
| `si_vector` | seven comma-separated integers (SI base exponents), e.g. `"0,0,-1,0,0,0,0"`. Required with `dimension_kind='si'`, refused without it |
| `canonical_unit`, `value_type`, `allowed_values`, `standard_ref`, `higher_is_better` | descriptive, stored as given |
| `display_unit` | the unit people expect for a measurand (`Å`, `eV`, `%`, `µmol h⁻¹ cm⁻²`); measures are stored in SI (`canonical_unit`) and shown in this unit. A unit pint reads with the same dimension as `canonical_unit`; a `canonical_unit` pint cannot convert (`USD`, `pH`) allows only itself. See `precis-measure-help` |
| `includes`, `excludes` | lists of non-empty strings: boundary examples that belong, and near-misses that do not (each naming the node it belongs to). Shown on `get`, not searched. How to write them: `precis-classify-help` |
| `start`, `contract` | `start=true` marks a root; `contract={"required_keys": [...]}` only on a start node |
| `legacy_source`, `applies_to_ref` | provenance and a pointer to the subject node |
| `required_conditions` | list of condition names a measure of this term must have among its input rows (e.g. `["product", "potential"]`); allowed on any node, a descendant inherits its ancestors' names along `specialises`. A missing one flags the measure, never refuses it. Not the same as `contract.required_keys`, which binds taxon meta |

## Put a node under a parent

"Y specialises X" means Y is the narrower term and X the broader one. Link the
narrow node to the broad one; `rel='generalises'` written the other way round
is the same edge.

```python
put(kind="taxon", text="faradaic efficiency — the fraction of charge that goes to the product",
    meta={"dimension_kind": "dimensionless"},
    link="taxon:12", rel="specialises")
link(kind="taxon", id=40, target="taxon:12", rel="specialises",
     meta={"axis": "composition"})
```

`meta={"axis": ...}` on `link` names the angle of the edge (any non-empty
string); a node may have parents on different axes. Re-linking the same pair
with a new axis replaces it. `meta=` on `link` is accepted for taxon only;
other kinds refuse it.

A link is refused, writing nothing, when:

- either end is not a taxon (the refusal names the handle and kind; to attach
  another kind of ref to a taxon use `rel='instance-of'`);
- it would make a cycle, or links a node to itself (both ends are named);
- the target is a chunk (`~N`) rather than a whole node;
- a start node above the new parent requires a key the node lacks. Each start
  node's `contract.required_keys` binds every node beneath it, however many
  levels down, so give the key in `meta=` on put: `put(..., meta={"dimension_kind": "si", "si_vector": "..."}, link=...)`.

## Read the hierarchy

`get(kind="taxon", id="tn12")` adds one line of direct parents
(`specialises: tn3 rate [composition]`) and a child count.
`get(kind="taxon", id="tn12", view="path")` prints one chain per route up to a
start node, start node first, each hop as `<handle> <name> — <first sentence of
the definition>` with the edge's axis between hops. A node reaching no start
node says so and prints its partial chains; chains beyond 20 are counted, not
shown. `get(kind="taxon", id="/unrooted")` lists every node that reaches no
start node, or says none do. `get(kind="taxon", id="/unmapped")` lists the
nodes seeded from the legacy registries whose dimension could not be mapped
(`dimension_kind` empty), so a curator can set it.

## Address a node

In `get`, `search(under=)` and `link(kind='taxon', target='taxon:...')`:
`42`, `tn42`, `taxon:42`, or a path of names, slugs or aliases joined by `/`
and never starting with one: `measurand/temperature` means a node
`temperature` whose parent is `measurand`. A lone `temperature` is a plain
term lookup. Several matches are refused with each candidate's handle and
path; no match lists near candidates. A leading `/` is a list view
(`/unrooted`, `/unmapped`, `/recent`). A link from another kind
(`rel='instance-of'`) and create-time `put(link=)` take the id only.

## Search

`search(kind="taxon", q=...)` matches the name and the definition, lexically
and by meaning; a word that appears only in the definition finds the node
even with the embedder down.

```python
search(kind="taxon", under="measurand/temperature", axis="method", depth=2)
search(kind="taxon", under="tn12", q="calorimetry")
```

`under=` returns the descendants of that node, never the node itself.
`axis=` keeps only edges whose axis equals it (every hop); `depth=N` means at
most N hops (default unbounded). Without `q=` the set is listed by depth,
then name, with each node's first definition sentence; with `q=` the ranked
hits are cut to that set. `axis=`/`depth=` without `under=` are refused, and
other kinds refuse all three.

## Status is earned

A new taxon is `proposed`. `put(meta={"status": "systematic"})` is refused;
`systematic` is reached by usage, not by asking.

## Read

`get(kind="taxon", id="tn12")` shows the name, definition, aliases, status,
dimension, start-node marker and tags. `search(kind="taxon", q="how fast the
catalyst cycles")` matches on the definition.

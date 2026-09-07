---
id: precis-rxn-help
title: precis — reaction facts (rxn)
summary: record what a transformation actually did — yields, conditions, catalyst — sourced to a paper or patent, and read the SPREAD across sources rather than one number
answers:
  - how do I record a reaction yield from a paper?
  - what yields do amide couplings actually give?
  - how do I find precedent for a step in a route I am proposing?
  - why does one reaction show five different yields?
  - how is `rxn` different from `route` and `pathway`?
applies-to: get/put/search (kind='rxn')
status: active
---

# precis-rxn-help — reaction facts

A `rxn` is **one transformation plus everything anyone has reported about it**.
Not a plan, not a network: a fact store. You write a reaction SMILES once, then
append sourced values — a yield from this paper, a different yield under
different conditions from that patent — and reading it back gives you *all of
them*, each with its conditions and its citation.

**The spread is the finding.** Three papers reporting 45 %, 71 % and 88 % is
more informative than any average of them, and the conditions explain the gap.
Nothing in this kind collapses that into a single number, and you should not
either.

## Three kinds, three different questions — do not confuse them

| kind | question | shape |
|---|---|---|
| **`rxn`** | *what has this transformation actually done?* | sourced facts, from the literature |
| `route` | *how do I get to this molecule?* | a planned retrosynthesis graph |
| `pathway` | *what happens on this catalyst surface?* | a computed reaction network |

`pathway` uses the word "reaction" for its own edges. That is why this kind is
`rxn`.

## Record a reaction, then add what sources report

```python
put(kind='rxn', id='fischer-etoac',
    title='Fischer esterification → ethyl acetate',
    rxn_smiles='CC(=O)O.OCC>>CC(=O)OCC.O')

put(kind='rxn', id='fischer-etoac',
    property='yield', value=83, unit='%',
    conditions={'solvent': 'toluene', 'catalyst': 'H2SO4',
                'yield_type': 'isolated'},
    method='measured', source='paper:my-paper', chunk='pc67890')
```

The second call is the whole point: **one sourced fact, chunk-precise**. Run it
again from another paper and *both* rows persist.

`rxn_smiles` accepts `reactants>>products` or `reactants>agents>products`.
Agents (catalyst, solvent) are recorded but excluded from identity — the same
transformation with a different catalyst is the same transformation, and the
catalyst belongs in a value's `conditions` where it can be compared.

## Read it

```python
get(kind='rxn', id='fischer-etoac')          # the page, grouped by property
get(kind='rxn', view='properties')           # the registry
```

Each property block says `sourced: N of M` — how much of what you are reading
is actually grounded. A block where that reads `0 of 3` is three numbers
nobody can check.

## Find precedent — the read this kind exists for

```python
search(kind='rxn', q='amide coupling')
search(kind='rxn', property='yield', min=70, reaction_class='RXNO:0000024')
```

The second form is the one that matters when you are **proposing a route to a
molecule nobody has made**. An exact-reaction lookup will never hit — that
molecule does not exist. But the *transformation* is known, so ask what that
class of step actually yields, and on what substrates.

## Two identity keys, and why

Every reaction carries both, derived from the canonicalised SMILES:

- **`uid_transform`** — over `(reactants, desired product)`, **ignoring
  byproducts**. This is what converges precedent, because the literature
  records byproducts inconsistently: `A.B>>C` and `A.B>>C.NaBr` are the same
  chemistry and share this key.
- **`uid_strict`** — over the whole balanced equation. Exact identity, used to
  collapse re-imports of the same record.

Writing a reaction whose `uid_transform` already exists prints a note pointing
at the sibling — usually you want to add values *there* rather than create a
near-duplicate.

## `method=` is not optional decoration

`measured` · `patent-extracted` · `literature-reported` · `predicted` ·
`estimated` · `computed`.

This is the axis that keeps a hand-curated yield distinguishable from one
text-mined out of a patent at scale. Without it the coverage line lies: a
`sourced: 5 of 5` built from five bulk-extracted rows is not the same evidence
as five read papers.

## `source_licence=` before you publish anything

A value from a non-commercial or non-redistributable source must never reach a
nanopub. Record the licence on the row (`CC0`, `CC-BY-4.0`, `CC-BY-NC-SA-4.0`,
…) so the constraint travels with the number instead of living in someone's
memory. Unrecorded means **treat as unpublishable**.

It is rendered, not just stored: both the reaction page and the range-filter
search carry a `licence` column, so you can see what you are allowed to do with
a number at the moment you read it. A `—` there is not "no restriction", it is
"nobody recorded one".

## Sourcing rules

`source=` takes `paper:<slug>`, `patent:<slug>`, `datasheet:<slug>`, or a bare
URL. `chunk=` needs `source=`. Patents count here where they do not for
`material` — patents are where the reaction literature actually lives.

A `rxn` ref does **not** ground a claim by itself. Claims still cite the paper
chunk through the normal hub path; the reaction carries the numbers. That is
deliberate — it keeps one citation ladder.

## SMILES survive the round trip — but watch two boundaries

**Passing them in is safe.** MCP arguments are JSON, and every real SMILES
round-trips intact, including the backslash in `C/C=C\C`. You do not need to
escape anything on the way in. Pass the SMILES exactly as written.

**Reading them back, they arrive in code spans.** `get` and `search` wrap every
SMILES and every condition value in backticks. That is not decoration: a
stereocentre followed by a branch — `C[C@H](N)C(=O)O`, alanine, and much of
drug-like space — matches markdown's inline-link grammar `[text](target)` and
would otherwise render as a **link**, silently deleting the structure from what
you read. Ligand notation does it too (`Pd[P(t-Bu)3](OAc)2`). Strip the
backticks before feeding a SMILES to anything that parses it; what is stored is
always the bare canonical string.

**Two places it can still bite you, both outside this kind:**

- **A shell.** Nearly every SMILES contains a shell metacharacter, and a
  reaction SMILES contains `>>` — which redirects. Single-quote it, always.
- **YAML.** A bare atom symbol is not a string: `NO` parses as *false*, `ON` as
  *true*. Quote every chemical label in a YAML config. (`pathway` configs hit
  this constantly — see `precis-pathway-help`.)

## The discipline

- **Never invent a yield.** Absent renders `—`. The coverage line exists so
  that a confident total built from two numbers and five guesses is impossible
  to state.
- **Conditions are part of the claim.** "78 %" without solvent, temperature and
  scale is false by omission. A yield at 1 mmol and at 1 mol are different
  claims.
- **Do not average.** If you need a central value for a calculation, say which
  rows you used and why.
- **Ground the lever before proposing it.** Search the corpus for the
  transformation, cite what you lean on, then record the value.

## Not in this slice

Route scoring, cost vectors, stock termination, and bulk import are later
rungs — see `docs/backlog/reaction-kind-and-synthesis-cost.md`. Today `rxn` is
the fact store those will read from.

## See also

```python
get(kind='skill', id='precis-material-help')  # the same star schema, for substances
get(kind='skill', id='precis-lab-help')       # chaining route/protein/structure
get(kind='skill', id='precis-search-help')    # grounding in the paper corpus
```

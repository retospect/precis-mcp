---
id: precis-measure-help
family: science
title: precis — measures (one sourced number per row, any subject)
summary: the measure record — literal-first, one canonical unit per measurand, tiers, reference states, runs with their input conditions, flags, and append-only rows
answers:
  - what is a measure and how does it differ from a material or component value?
  - why was a measure flagged instead of refused, and what does the flag mean?
  - why is a unit refused for a measurand, and how are mass rates converted to amount rates?
  - what does tier measured require, and why is a cited number never measured?
  - how do I correct a measure that is already stored?
  - how does a measure point at the paper chunk that prints it?
  - how do I find every measure of a property in a numeric range, in the unit I think in?
  - how do I filter measures by a run's conditions (quantisation, hardware, product, potential)?
  - how do I read the best value per measurand for a quest?
  - why does a value show as 140 pm or 95 % and what is stored?
  - how do I write a measure run (an output and its input conditions) with put?
  - how do I review a measure row, and why is a review with no model refused?
  - how is a batch of claims extracted into reviewed measures?
applies-to: put / edit of kind=measure; extraction passes that write measures; reading and searching measures
tags: design, external-sources
kinds: material, component, taxon, measure, quest
status: active
---

# precis-measure-help — measures

A **measure** is one sourced number about a subject ref: a paper, a material,
a component, a structure, a design. `material` and `component` values are
measures; their `put` verbs are unchanged and write the same record. Measures written for a
material or component subject appear in those value views only when the measurand
was seeded from the legacy registry.

## Find and read measures

Rows are not refs, so they have their own kind, `measure`, addressed by the row
id (`12`, or the handle `mx12` that search lines print).

```python
get(kind="measure", id=12)  # one row, with everything a review needs
search(kind="measure", property="measurand/faradaic-efficiency", min=90, unit="%",
       q="product=NH3 potential<-0.5 V")
search(kind="measure", property="tn42", min=1.4, max=2.0, unit="Å")
search(kind="measure", q="quant=Q4 decode", property="measurand/decode-speed")
get(kind="quest", id=7, view="measures")  # best live value per measurand, one line per group
```

**`get(kind='measure', id=N)`** shows the literal (and its reported unit), the
value in the display unit with the SI value beside it, the measurand (taxon
handle, name and path), the subject (ref handle and title), its label and run
key, the run's **conditions** (its input rows, each in its own display unit),
tier / attribution / measurand status / extraction status, the **anchor** (paper
handle, chunk handle, scheme and span), the ledger **reviews** newest first
(each `current`, or `STALE` when a fixed field changed since the review) and
the supersession chain. The fisheye ladder is `Unsupported`: a measure is a row,
not a node.

**`search(kind='measure', …)`** takes:

| arg | meaning |
|---|---|
| `property=` | the measurand: a taxon handle (`tn42`, `taxon:42`) or path (`measurand/faradaic-efficiency`); covers the taxon and every `specialises` descendant |
| `min=` / `max=` | numbers, in `unit=` (else the taxon's `display_unit`, else SI); converted to SI, then matched by **interval overlap** on low/high (a point is a one-value interval, `<x` reaches down from x, `>x` up). Need `property=` |
| `unit=` | the unit of `min`/`max` **and of the output**; a unit of another kind than the measurand is refused, naming both |
| `q=` | condition terms and subject words (below) |
| `status='all'` | also superseded, ambiguous, escalated and anchor-lost rows, each marked; default is live rows only |

`q=` terms are `name=value`, `name<value`, `name>value` (also `<=`, `>=`),
matched against the run's input rows by condition name (the row's own
condition label, or its taxon's name, slug or alias). A numeric value may carry
a unit (`potential<-0.5V`, `temperature>300 K`); a bare number is read in that
input's display unit, else SI. A text value compares by slug (`hardware=M2-Ultra`
equals `M2 Ultra`); quote one with spaces: `hardware="M2 Ultra"`. Every other
word must occur in the subject label. Results are output rows only, one line
each: `mx12 | subject | measurand: value | conditions | tier | paper handle`.

`get(kind='quest', id=Q, view='measures')` lists, for everything that serves
the quest at any depth, the best live value per `(measurand, reference,
normalization)` group, with its conditions, tier, paper and current review state
(`approved`, `proposed` or `unreviewed`). The direction comes
from the taxon's `higher_is_better`; a group without it shows its row count and
no best. Per-area and per-mass yields, or RHE and SHE potentials, are separate
lines: groups are never compared. Left out of every ranking: ambiguous
measurands, flagged rows (`meta.escalation`), anchor-lost `measured` rows,
`trusted = false`, rows whose newest current review is `rejected`, and anything
that is not a single number (an interval, a bound, a category).
A potential stated against SHE does not stand in for an RHE
one: conversion between references is not built.
Only reviews matching the row's current content hash count, newest first
(review id breaks timestamp ties). A later current approval restores
eligibility; approval does not outrank a better proposed or unreviewed value.

## Display: SI stored, your unit shown

Every value is stored in its measurand's canonical unit, which is the coherent
SI unit with no prefix (eV is stored as J, % as a fraction, Å as m, °C as K,
mA cm⁻² as A m⁻²). It is shown in, in order: the `unit=` you pass; the taxon's
`display_unit` (`Å`, `eV`, `%`, `µmol h⁻¹ cm⁻²`); else SI with an automatic
prefix, so 1.4e-10 m reads `140 pm`, never `0.00000000014 m`.

- A fraction (canonical unit `1`) shows bare (`0.95`); `95 %` appears only when
  the taxon's `display_unit` is `%` (or you pass `unit='%'`).
- pH (and pOH, pKa, dB) is a scale: never prefixed, never converted.
- Affine units convert as absolute values: 298.15 K with display `°C` is
  `25 °C`. A temperature difference is its own measurand.
- `display_unit` is a taxon meta key (see `precis-taxon-help`): a unit of the
  same dimension as `canonical_unit`.

## One measure, one run

A measure is written as a **run**: one *output* (the number a claim reports)
plus exactly its own *input* rows (set temperature, potential, product, ...)
sharing one run key. "95% at -0.5 V and 61% at -0.9 V" is two runs, each with
its own potential. An input row has a role: `context` (conditions of the
observation), `preparation` (how the sample was made) or `model` (an analytical
assumption). Only `context` rows satisfy a measurand's required conditions.

## Row shape

| field | meaning |
|---|---|
| `literal` | the exact printed string, always kept (`9.6 ± 1.7`, `<1`, `550–575`, `Cordierite`) |
| `value_num` / `value_low` / `value_high` / `value_err` / `value_text` / `value_bool` | the parsed reading. **Stored** in the measurand's canonical (SI) unit; **on a write** you give them in the REPORTED unit, the same unit as `reported_unit` and the `literal`, and put converts them (literal `500`, `reported_unit` `mV`, optional `value_num` 500 stores 0.5 V; `value_num` 0.5 with `mV` would store 0.0005 V) |
| `value_form` | `point`, `approximate_point`, `upper_bound`, `lower_bound`, `interval`, `categorical`, `boolean`, `not_established` |
| `reported_unit` | the unit as printed; required for a number on a measurand with a canonical unit (a table-recipe row names the canonical unit); NULL only for a unitless measurand and for rows written before 0188 |
| `reference` | the reference state (RHE, SHE, Ag/AgCl, ...), never folded into the unit |
| `normalization` | the basis (per catalyst mass, per geometric area, per ECSA); never compare across different bases |
| `subject` / `subject_group` | the paper-local sample label ("Cu NWA") and its group (the claim handle) |
| `tier` | `measured`, `computed`, `derived`, `asserted`; NULL means it could not be established |
| `source_attribution` | `own_work`, `cited_work`, `not_established` |
| `measurand_status` | `explicit`, `interpreted`, `ambiguous` |
| `extraction_status` | `unverified`, `anchor_matched`, `anchor_mismatch`, `human_checked`; computed when written |

The measurand is a `taxon` node. Each carries one canonical unit and values are
stored in it.

## Units

The reported unit is parsed (Unicode superscripts, `·`, `mg_cat⁻¹`) and
converted to the measurand's canonical unit.

- A unit with no dimension match is **refused**, naming both units.
- A number with a reported unit on a measurand that has no canonical unit is
  **refused**: values are stored normalised to one unit per measurand, so set the
  measurand's canonical unit first. A unitless number (a count, a ratio, a
  categorical or boolean value) lands on a measurand that has no canonical unit.
- A number with **no** reported unit on a measurand that has a canonical unit is
  **refused**, naming the canonical and the display unit: `95` is 95 % or 0.95,
  `5` is 5 mm or 5 m, and neither is guessed. Give the unit it is printed in, or
  the canonical unit when it already is in it.
- A mass rate against an amount-rate measurand (`µg h⁻¹ cm⁻²` against
  `mol s⁻¹ m⁻²`) converts through the molar mass of the run's `product` input
  row (its formula, e.g. `NH3`). No formula: the row is stored with no parsed
  value and flagged `no_molar_mass`.
- A subscript basis label on a denominator (`mg_cat⁻¹`, `mg_Fe⁻¹`) is moved into
  `normalization`.

## Evidence

A row names where its number is printed: `anchor_scheme` (`sentence`, a range, a
numeric atom, or raw offsets) and `span` (the string, or `[chunk, start, end]`).
It also points at a `quantifies` edge from the paper chunk to the measurand
node. Every measure on the same chunk and measurand shares that one edge and
keeps its own span on its row. More than one span per result: the extra edges
are listed in `meta.extra_anchors` as `{link_id, anchor_scheme, span}`.

- A `measured` row whose anchoring link is gone (the chunk or paper was
  deleted) is **anchor-lost**: it stays, shows as such when read, and ranking
  reads skip it until it is re-anchored (not built yet). Its review goes stale.
- `tier='measured'` is **refused** without an anchor on a paper chunk, and refused
  when `source_attribution='cited_work'` (a restated number is `asserted`).
- `extraction_status` is `anchor_matched` when the literal occurs in the span's
  text, `anchor_mismatch` when it does not (flagged, never refused),
  `unverified` with no anchor. Raw offsets test the slice; other schemes test the
  whole chunk.

## Flags

A measure is never refused for a missing condition. A measurand lists
`required_conditions` (on itself or any ancestor along `specialises`); a run
whose context inputs lack one is stored with `meta.escalation` naming it, and
readers that rank values skip flagged rows.

## Corrections

Rows are append-only. Wrong or updated numbers are replaced by a new row that
points at the old one (`supersedes`); the old row stays as history and drops out
of live reads. The replacement must state the same number: same subject, subject
label, measurand and direction. Only `trusted`, the first `superseded_by` /
`superseded_at` pair and `extraction_status` set to `human_checked` ever change
on a stored row. `meta.extra_anchors` is written when the row is written.

A measurand's canonical unit, dimension kind and SI vector cannot be changed
while it has live measures. Measures are never deleted, and a ref with measures
about it (or measuring it) cannot be hard-deleted. Merging a ref that has
measures about it or anchored to it is refused, naming the count; re-pointing
measures onto the survivor is not built yet.

## Write a run

`put(kind='measure')` writes one run: the output's printed literal in `text=`,
its fields in `meta=`, the input rows in `items=`. All in one transaction; a
refusal writes nothing.

```python
put(kind="measure", text="95", reason="qu202467 pilot extraction",
    meta={"measurand": "measurand/faradaic-efficiency", "subject": "pa12",
          "subject_label": "Cu NWA", "subject_group": "fi345",
          "reported_unit": "%", "reference": "RHE", "tier": "measured",
          "anchor": {"chunk": "pc678", "anchor_scheme": "sentence", "span": "s3"}},
    items=[{"measurand": "measurand/potential", "literal": "-0.5",
            "reported_unit": "V", "reference": "RHE"},
           {"measurand": "measurand/reaction-product", "literal": "NH3",
            "condition": "product"}])
```

- **`meta`** (the output): `measurand` (taxon handle, path or id) and
  `subject` (ref handle or id) are required; optional `subject_label`,
  `subject_group`, `reported_unit`, `value_num` / `value_low` / `value_high` /
  `value_err` / `value_text` / `value_bool` / `value_form`, `reference`,
  `normalization`, `normalization_status`, `tier`, `source_attribution`,
  `measurand_status`, `anchor`, `extra_anchors`, `supersedes` (a measure id or
  `mx` handle), `derived_from` (a list of them), `note` (free text, at most 500 characters;
  stored in `measures.meta`, shown by `get`, fixed once written), `run_key`
  (else minted) and `model` (the writing model).
- **`anchor`** is `{chunk: <chunk handle or id>, anchor_scheme, span}`; the paper
  is the chunk's own. `tier='measured'` needs one.
- **`items`**: each input row takes the same fields plus its own `literal`
  (`text` is accepted as the same word) and `condition` (the label that
  satisfies a required condition, e.g. `product`). An input inherits the
  output's `subject`, `subject_group` and `anchor` unless it gives its own;
  `direction` is `input` (default) or `covariate`. Items take `note` too.
- **Errors** are `BadInput` naming the field (`items[1].measurand: ...`) with a
  `next:` example; an unknown field is refused with the accepted list.
- **`reason=`** goes to the revision context like `edit`'s.
- **Answer**: the run key, one line per written row (`mx12 output ...`), then
  `flagged:` lines for anything flagged, each with its reason: a
  `meta.escalation` entry (a missing required condition, no molar mass) or
  `anchor_mismatch` (the literal is not in the anchored span: check the chunk).

## Review a row

```python
edit(kind="measure", id="mx12", review="model", verdict="approved",
     text="95 is printed in the anchor sentence", meta={"model": "claude-opus-5-5"})
```

- `review=` is the reviewer's kind, `human` or `model`; `verdict=` is `approved`
  or `rejected` (it defaults to `approved`: always pass it); `text=` is the
  note; `meta={'model': ...}` names the model (`version` optional).
- The review is taken at the row's sha now: a later change to a fixed field
  makes it `STALE` in `get`. Reviews append; a second one never replaces the first.
- **A model review must name its model** (`meta.model`), or it is refused.
- **A human review** is a sign-off the session relays, as with
  `edit(kind='draft', review='human')`: `edit(kind='measure', id=12,
  review='human', verdict='approved', meta={'actor': 'reto'})`. The actor is
  `meta.actor`, else `human`; the model is always NULL, and a `meta.model` on a
  human review is refused ("a human review has no model").
- The ledger's actor is plain text, so an agent could claim to be a person.
  Relay a human sign-off only when the person gave it; the record is a claim,
  not proof. A web or CLI caller sets the actor through
  `revision_context(actor=...)` instead.
- The answer is the review id, the verdict and the sha. `get(kind='measure',
  id=12)` lists it under `reviews:`.
- **No other edit exists.** `edit(kind='measure')` without `review=` is
  refused: rows are append-only. Correct a number with a new `put` carrying
  `meta.supersedes=<mx id>` (same measurand, subject, label).

The review ledger is the shared one (target kind `measure`) and covers the fixed
fields only, so setting `trusted` never makes a review stale.

## Extraction pass (an operation, not code)

How a corpus of claims becomes reviewed measures. Nothing here is a worker.

1. **Propose.** Subagents at the mid tier each read one finding with its
   anchored chunk text and write the runs they see as JSON: one run per claim
   and condition set ("95% at -0.5 V and 61% at -0.9 V" is two runs), each
   holding exactly the `put` arguments above. The measurand is a taxon from
   `search(kind='taxon', ...)`; one that is missing is minted first.
2. **Check.** A script reads every proposal and tests it before any write: the
   literal occurs in the chunk text on number boundaries, the measurand and
   subject resolve, the unit fits the taxon's `canonical_unit`, a `measured`
   run has its anchor. Failures go back to the proposer; nothing is written.
3. **Write.** The checked proposals are `put` through `scripts/prod-precis`
   with `reason='<quest id> pilot extraction'`, one call per run. Keep the
   returned `mx` ids and flags in a log beside the input.
4. **Review.** A reviewer subagent on a larger model reads each written row
   with `get(kind='measure', id=N)` and its chunk, then `edit`s it with
   `review='model'`, a `verdict` and a one-line note. Rows it rejects are
   corrected by a new `put` with `meta.supersedes`; anything it cannot decide
   goes to a person.

Report what was left out (claims with no anchor, no number) rather than
extracting them.

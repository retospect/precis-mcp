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
search(kind="measure", args={"paper": "pa40"})  # rows printed in one paper
search(kind="measure", args={"serves": "quest:500066"})  # papers serving a quest
search(kind="measure", property="measurand/activation-barrier", min=50,
       unit="kJ/mol", args={"serves": "quest:500066"})  # filters compose
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

Table-derived anchors also show the logical table, cell row and column, and
header from `meta.table`. A transformed table value shows its transform and
printed value (for example, `transform: log10 (printed 13)`).

**`search(kind='measure', …)`** takes:

| arg | meaning |
|---|---|
| `property=` | the measurand: a taxon handle (`tn42`, `taxon:42`) or path (`measurand/faradaic-efficiency`); covers the taxon and every `specialises` descendant |
| `min=` / `max=` | numbers, in `unit=` (else the taxon's `display_unit`, else SI); converted to SI, then matched by **interval overlap** on low/high (a point is a one-value interval, `<x` reaches down from x, `>x` up). Need `property=` |
| `unit=` | the unit of `min`/`max` **and of the output**; a unit of another kind than the measurand is refused, naming both. A currency unit (`unit='EUR/t'`, `unit='USD2015/t'`) bounds within that one currency **and price year** only — no year is its own group. With **no** `unit=` the taxon's currency bounds across *every* price year and each row labels its own (`USD(2010)/t`); either way rows in another currency are left out of the range and counted in a footer line |
| `q=` | condition terms and subject words (below) |
| `args={'paper': …}` | subject or anchor chunk belongs to this paper; handle, id or cite-key slug |
| `args={'serves': 'quest:<id>'}` | subject or anchor paper serves the quest, directly or through sub-quests |
| `status='all'` | also superseded, ambiguous, escalated and anchor-lost rows, each marked; default is live rows only |

These scope args work without `property=` or `q=` and intersect with each
other, measurand, range and condition filters.

`q=` terms are `name=value`, `name<value`, `name>value` (also `<=`, `>=`),
matched against the run's input rows by condition name (the row's own
condition label, or its taxon's name, slug or alias). A numeric value may carry
a unit (`potential<-0.5V`, `temperature>300 K`); a bare number is read in that
input's display unit, else SI. A text value compares by slug (`hardware=M2-Ultra`
equals `M2 Ultra`); quote one with spaces: `hardware="M2 Ultra"`. Every other
word must occur in the subject label, the measurand name, the literal,
or the quantified-by anchor chunk text. Results are output rows only, one line
each: `mx12 | subject | measurand: value | conditions | tier | paper handle`.

## Rank measures for a quest

`get(kind='quest', id=Q, view='measures')` lists, for everything that serves
the quest at any depth, the best live value per `(measurand, reference,
normalization, currency, price_year)` group — a NULL year is its own group —
further partitioned by the measurand's
*categorical*
required conditions — a Faradaic efficiency ranks per product, so a line reads
"FE vs RHE · product=NH3" — with its conditions, tier, paper and current
review state
(`approved`, `proposed` or `unreviewed`). The direction comes
from the taxon's `higher_is_better`; a group without it shows its row count and
no best. Per-area and per-mass yields, RHE and SHE potentials, or EUR and USD
costs, are separate
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
- A **currency** row is the exception to "stored in SI": the number stays in
  the *reported* currency and only the remainder scales — `1 EUR2015/kg` on a
  `USD/t` measurand stores `1000` with `currency='EUR'`, `price_year=2015`,
  and shows as `1000 EUR(2015)/t`. Currencies never convert between each
  other.

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
- A **currency unit** carries an ISO 4217 code (`USD`, `EUR`, `GBP`, `JPY`,
  ... — every code the ECB euro reference rates quote, plus EUR) or one of
  the symbols `$`/`US$`/`€`/`£` (mapped to USD/USD/EUR/GBP; `¥`, `A$` and
  `C$` are refused as ambiguous — give the code; a `$...$` pair whose
  contents carry TeX, like `mA$^{-1}$`, is a math delimiter, never a
  dollar — `$/t (2015$)` is two dollars and reads as USD 2015). An optional
  4-digit price
  year sits next to the code: `EUR2015/t`, `EUR 2015`, `EUR(2015)`,
  `2015 EUR`; or pass `price_year=` in meta. Only 1900–2100 reads as a
  year — `1000 USD/yr` is a scale factor and is refused; state the plain
  unit with the number already scaled. A year in both places that
  disagrees is refused; a year on a non-currency figure is refused; a
  currency measurand whose `reported_unit` carries no token is refused with
  `reported_unit='EUR2015/t'` as the hint. No year is accepted — the answer
  reminds you the row is shown as reported, never converted, until a
  `price_year=` supersession lands.

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
refusal writes nothing. One more refusal: a live output row with the same
measurand, subject, anchor scheme, span and value is a **duplicate** — the
number is already written; `get` it, or re-write the run with
`meta={'supersedes': id}` when it should replace that row.

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
  `subject_group`, `reported_unit`, `price_year` (the year a currency figure
  is priced at, when the unit token does not say it), `value_num` /
  `value_low` / `value_high` /
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
  `meta.escalation` entry — a missing required condition, no molar mass, an
  `implausible_value` (the canonical value sits 3+ orders of magnitude off the
  measurand's live median — a stripped µ looks exactly like that) or
  `glyph_suspected` (the source paper's extracted text is flagged) — or
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

The same pass also runs as two jobs. `put(kind='job', job_type='survey_extract',
params={'paper': <id>})` reads one paper's chunks on the local model, writes
the checked runs tagged with a batch token in `meta.extraction_run`, then
spawns `measure_accept`. `measure_accept` has a frontier model review a
stratified sample of that batch and accepts or fails the batch by the Wilson
rule (default bar 5%); the verdict lands in the accept job's
`meta.acceptance`. Run `measure_accept` on its own (`params={'paper': <id>,
'extraction_run': <token>, 'sample_n': N, 'bar': 0.05}`) to re-accept a batch
without extracting again. `paper` may be omitted — the batch is then the
whole run token across papers and the sample stratifies by paper × measurand
(the intended shape: one token per trial or filler window, since a per-paper
batch rarely reaches the n=52 the zero-error bar needs).

The verdict is read, never written onto rows (a row's `meta` is frozen).
Each `meta.acceptance` records its coverage, frozen at the draw:
`papers` — the papers in the sampling frame — and `max_id`, the frame's
row-id high-water mark (rows minted under the token after the draw were
never in the frame). A run-level accept (`extraction_run` without
`paper`) is refused while `survey_extract` jobs still carry the token —
sequence order is not commit order, so a row can hold an id under the
mark yet commit after the frame read.
`Store.accepted_runs(tokens)` maps each token to the latest *counting*
verdict per covered paper: a verdict counts only with both coverage
fields, `bar` at or under the default 5%, and `z` at or above the
default 1.645, so a loose-bar or loose-z re-accept can neither grant nor
overturn and a pre-coverage verdict grants nothing.
`best_measure(..., accepted_only=True)` keeps a token row only when its
paper's governing verdict passed, the row id sits under the mark, and the
row keeps its anchor — rows carrying no `extraction_run` token are not
batch rows and stay.

For parsed pipe tables, the pass makes one table-level recipe call and expands
its series into cell-anchored runs; an unparsed table or unusable recipe falls
back to per-cell reading. Table outputs and inputs carry `meta.table` cell
coordinates, header, orientation and caption link. Log-scale series record
`meta.transform` (`log10` or `ln`), store the transformed value, and retain the
printed exponent as `meta.printed_value`.

---
id: precis-measure-help
title: precis — measures (one sourced number per row, any subject)
summary: the measure record — literal-first, one canonical unit per measurand, tiers, reference states, runs with their input conditions, flags, and append-only rows
answers:
  - what is a measure and how does it differ from a material or component value?
  - why was a measure flagged instead of refused, and what does the flag mean?
  - why is a unit refused for a measurand, and how are mass rates converted to amount rates?
  - what does tier measured require, and why is a cited number never measured?
  - how do I correct a measure that is already stored?
  - how does a measure point at the paper chunk that prints it?
applies-to: material / component value reads; extraction passes that write measures
tags: design, external-sources
kinds: material, component, taxon
status: active
---

# precis-measure-help — measures

A **measure** is one sourced number about a subject ref: a paper, a material,
a component, a structure, a design. `material` and `component` values are
measures; their `put` verbs are unchanged and write the same record. Measures written for a
material or component subject appear in those value views only when the measurand
was seeded from the legacy registry.

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
| `value_num` / `value_low` / `value_high` / `value_err` / `value_text` / `value_bool` | the parsed reading, in the measurand's canonical unit |
| `value_form` | `point`, `approximate_point`, `upper_bound`, `lower_bound`, `interval`, `categorical`, `boolean`, `not_established` |
| `reported_unit` | the unit as printed; NULL when the number is already canonical (a table-recipe row) |
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
  categorical or boolean value, or a number with no reported unit) lands.
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

## Review

A measure is reviewed through the shared review ledger (target kind `measure`).
The review covers the fixed fields only, so setting `trusted` never makes it
stale; a stale measure review means a fixed field changed and is an alarm.

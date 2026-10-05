---
status: draft
title: Taxon facet navigation — classify-authoring guide, sibling disjointness, instance search under a node, facet view
pillar: memory-graph
prio: high
model: opus
---

# Taxon facet navigation — authoring guide first, then the queries

## Motivation / why

Reto, 2026-10-05: we will know thousands of things about AFM. An agent
must be able to search them from the AFM node ("all children that deal
with xyz") and walk them structurally (DFT of the tip vs cantilever
resonance corrections vs AFM images of nanotubes). Asked: how many
children should a node have, how do we query, how do we avoid overlap in
meaning, and which skills and rules make it work. He asked for the
authoring guide early, before the mesh fills.

Today the taxon tree exists (`specialises` with `meta.axis`), any ref
joins it by `instance-of` (migration 0173), and
`search(kind='taxon', under=, axis=, depth=)` walks the concept tree. The
gaps:

- `under=` is refused on every kind except taxon (`precis-taxon-help`
  §Search), so "every finding under AFM about xyz" cannot be asked.
- There is no rule for how writers classify, so the first thousand
  `instance-of` links will set the shape by accident.
- Nothing stops two siblings meaning nearly the same thing.

### The design, as argued in-session

**Concept children vs instances.** Only concept nodes (taxa) need a
fan-out rule. Instances (paper, finding, measure, part…) are unbounded:
they are filtered and searched, never listed whole.

**The reader is an LLM, so the limit is tokens and choice quality.** A
model reads 50–100 sibling labels in one listing (source needed: no
measurement of sibling-list size vs classification accuracy is filed);
it fails when siblings overlap in meaning, and when a long list buries
the middle (Liu et al. 2024, "Lost in the Middle: How Language Models Use
Long Contexts", TACL 12:157–173, doi:10.1162/tacl_a_00638, precis
paper 468137). So: split or merge on measured sibling confusion, not on
a child count; keep a soft cap of about 50 children per axis for token
cost only; every listing states its total and what it left out.

**Facets, not one tree.** "DFT of an AFM tip" is technique:AFM ∩
method:DFT ∩ object:tip, not a node. Within one axis an item has one
parent; across axes it has several. Combined concepts are computed as
intersections, and become nodes only when earned (class-lattice
`defined` classes). Drill-down over facet counts is the established
pattern for browsing large collections without a single hierarchy (Yee,
Swearingen, Li, Hearst 2003, "Faceted metadata for image search and
browsing", CHI '03, 401–408, doi:10.1145/642611.642681, precis paper
468138).

**Avoiding overlap in meaning — four layers:**

1. *One question per axis.* Each axis carries its question as text
   ("What method?"); every sibling on it must answer that question.
   Mixed-axis siblings are the main source of overlap. An axis declares
   itself `exclusive` (method, object, scale) or multi-label (topic);
   disjointness is enforced and measured only on exclusive axes, and
   each exclusive axis has an `other/unspecified` child so a classifier
   is never forced into the nearest wrong sibling.
2. *Definitions written against the siblings:* genus + differentia that
   names the sibling it excludes, 2–3 `includes` boundary examples, 2–3
   `excludes` near-misses each pointing at the node it belongs to,
   synonyms as aliases (never siblings).
3. *Refusal at mint time:* nearest-sibling definition similarity on the
   same parent and axis above a threshold refuses with candidates; a
   candidate that passes gets a discrimination test (a cheap model sorts
   5–10 real instances between it and its nearest sibling; coin-flip
   splits mean alias or sharper differentia).
4. *Measure and repair after the fact:* owned by `graph-gardener.md`
   (§"Sibling disjointness").

## In scope

Slices ship independently, in this order.

**Slice 0 — the authoring guide.** Landed 2026-10-05 except AC 0's
cold-agent check, which runs after deploy; the skill is the truth for its
rules now, and the list below is the original brief. New skill
`precis-classify-help`, the writer's and reader's rules, usable with
today's fields (definition text, aliases, `meta.axis`):

- *Writer:* attach one `instance-of` per relevant axis at the most
  specific node, never also to its ancestor; answer the axis question;
  read the siblings' excludes before choosing; use `other` instead of
  guessing; search aliases before minting; never mint a taxon to hold one
  item; never mint a combined (pre-coordinated) node.
- *Definition shape:* genus + differentia naming the excluded sibling;
  boundary examples in two new taxon meta keys `includes` / `excludes`
  (lists of strings, rendered on `get`, kept out of the embedded card so
  a near-miss naming a sibling does not pull that sibling's queries).
  Built with slice 0 (2026-10-05) instead of parsing definition text in
  slice 4.
- *Reader (browse protocol):* start at the node, read `view='facets'`
  (slice 3; until then `search(kind='taxon', under=, depth=1)` per
  axis), cut on the axis that splits the set most evenly, add `q=` only
  under about 50 items, cite the facet path used.
- `precis-taxon-help` and `precis-fisheye-help` point at it.

**Slice 1 — nearest-sibling refusal.** Extends the taxon dedup:
same-parent, same-axis definition similarity above a calibrated cutoff
refuses the mint and lists the candidates with handles.

**Slice 2 — `under=` on every kind.** `search(kind=<any>, under=<taxon>,
q=…)`: descendant closure, then the `instance-of` reverse edges, then
ranking by `q=`. `under=` takes a list for intersection
(`under=['technique/afm', 'method/dft']`).

**Slice 3 — `get(kind='taxon', id=…, view='facets')`.** For the
instances under the node, counts per value on every other axis.

- Default `sort='split'`: axes by entropy of their split (most even
  first), values by count; with `q=`, values by summed match score.
- `sort=` `recent` (instances added in the last 90 days), `evidence`
  (findings / verified measures, not raw papers), `gap` (two axes
  crossed, empty and thin cells first), `name`.
- About 8 axes and 15 values per axis, the rest summarised with counts;
  the footer names the other sorts with one-line purpose and steers to
  `under=<value>` instead of paging; `page=` for the full-list case.
- About 2k tokens per view.
- **Categorizer facets:** the closed ref-level tags from
  `data/axes/*.yaml` and `data/topics/*.yaml` (`DOMAIN`, `STUDYTYPE`,
  `SCALE`, `MATERIAL`, `PROPERTY`, `TRANSPORT`, `topic:`) appear as
  facets, labelled machine-written with the pass version, each with an
  `unclassified: N` row. Chunk-level `ROLE3:own` is a filter, not a
  count.

**Slice 4 — axis contract fields.** On the v1.5 axis taxon nodes:
`exclusive` flag, question text, the `other` child; `Includes:` /
`Excludes:` parsed from definitions into structured fields. Blocked by
`term-taxonomy.md` v1.5 (axis as taxon).

## Explicitly NOT in scope

- The confusion metric and the merge/sharpen/re-axis/re-parent repairs —
  `graph-gardener.md` §"Sibling disjointness".
- Defined classes and constraint hashes — `class-lattice-similarity-
  spaces-and-laws.md`; this item only routes combined concepts there.
- A closure table. The recursive walk stays until slice 3's facet counts
  breach the knowledge-mesh revisit trigger.
- Mapping categorizer axis values onto taxon nodes (one facet source) —
  term-taxonomy v1.5; slice 3 reads both sources until then.
- Fisheye ring changes beyond pointing at the skill.

## Acceptance criteria

0. `get(kind='skill', id='precis-classify-help')` serves; a cold agent
   given only the skill classifies 10 seeded AFM items into axis nodes
   with no ancestor double-links and no combined-node mint.
1. Minting a taxon whose definition paraphrases an existing sibling on
   the same axis is refused and names that sibling; an unrelated sibling
   mints.
2. `search(kind='finding', under='<node>', q=…)` returns only findings
   with an `instance-of` path into the subtree; a list `under=` returns
   the intersection; `under=` with a non-taxon target is refused.
3. `view='facets'` on a seeded node: axes ordered by split entropy,
   totals and hidden counts stated, categorizer facets marked
   machine-written with an `unclassified` row, `sort='gap'` lists an
   empty seeded cell first, output under the token budget.
4. An `exclusive` axis rejects a second `instance-of` from one item onto
   two of its siblings; a multi-label axis accepts it.

## Target + blast radius

Slice 0: `src/precis/data/skills/precis-classify-help.md`, pointers in
`precis-taxon-help.md` and `precis-fisheye-help.md`. Slices 1–4:
`handlers/taxon.py`, `store/_taxon_ops.py`, the search verb's `under=`
gate, `_link_tag_ops.py::check_relation_constraints` (slice 4's
exclusive check). Read-mostly; the only new refusals are at taxon mint
and `instance-of` link.

## Open questions / decisions log

- **[decided 2026-10-05, Reto]** File as one item plus a gardener
  amendment; rank as high as practical, slice 0 as an early authoring
  guide.
- **[open]** The nearest-sibling cutoff — calibrate together with
  term-taxonomy's `DEDUP_MAX_DISTANCE` follow-up, against hand-judged
  sibling pairs.
- **[open]** Whether slice 4's exclusive check refuses or warns on
  legacy links that already violate it.

## Sources

- Liu, Lin, Hewitt, Paranjape, Bevilacqua, Petroni, Liang (2024). Lost in
  the Middle: How Language Models Use Long Contexts. TACL 12:157–173.
  doi:10.1162/tacl_a_00638 — precis paper 468137.
- Yee, Swearingen, Li, Hearst (2003). Faceted metadata for image search
  and browsing. CHI '03, 401–408. doi:10.1145/642611.642681 — precis
  paper 468138.

---
status: draft
title: Taxon facet navigation — classify-authoring guide acceptance, axis contract fields
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

The taxon tree (`specialises` with `meta.axis`), `instance-of` joins
(migration 0173), `under=` on every kind, sibling refusal at mint and
`get(kind='taxon', view='facets')` exist. The open gaps:

- The classification guide and boundary-example fields are in source;
  served-skill readback and the cold-agent classification trial remain pending.
- Axes carry no contract (`exclusive`, question text, `other` child), so
  sibling disjointness is not enforced on `instance-of` links (slice 4).

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

Slice 0 acceptance and slice 4 remain.

**Slice 0 — authoring guide and boundary examples (source implemented;
acceptance pending).** `precis-classify-help` gives writer and reader rules
for definitions, aliases, `meta.axis` and `includes` / `excludes`:

- *Writer:* attach one `instance-of` per relevant axis at the most
  specific node, never also to its ancestor; answer the axis question;
  read the siblings' excludes before choosing; use `other` instead of
  guessing; search aliases before minting; never mint a taxon to hold one
  item; never mint a combined (pre-coordinated) node.
- *Axes:* the guide's six questions are recommendations for a general
  classification workflow. Reuse the parent/campaign's existing labels;
  composition, periodic, termination and material-class remain valid.
  No closed whitelist or legacy re-axis operation is introduced.
- *Definition shape:* genus + differentia naming the excluded sibling;
  boundary examples in two new taxon meta keys `includes` / `excludes`
  (lists of strings, rendered on `get`, kept out of the embedded card so
  a near-miss naming a sibling does not pull that sibling's queries).
  Validated by `taxonomy/nodes.py`, rendered by `handlers/taxon.py`;
  no definition parsing is needed.
- *Reader (browse protocol):* start at the node, read `view='facets'`,
  cut on the axis that splits the set most evenly, add `q=` only
  under about 50 items, cite the facet path used.
- `precis-taxon-help` and `precis-fisheye-help` point at it.

**Slice 4 — axis contract fields.** On the v1.5 axis taxon nodes:
`exclusive` flag, question text, the `other` child; reuse slice 0's
structured `includes` / `excludes`. Blocked by `term-taxonomy.md` v1.5
(axis as taxon).

## Explicitly NOT in scope

- The confusion metric and the merge/sharpen/re-axis/re-parent repairs —
  `graph-gardener.md` §"Sibling disjointness".
- Defined classes and constraint hashes — `class-lattice-similarity-
  spaces-and-laws.md`; this item only routes combined concepts there.
- A closure table. The recursive walk stays until the facet counts
  breach the knowledge-mesh revisit trigger.
- Mapping categorizer axis values onto taxon nodes (one facet source) —
  term-taxonomy v1.5; the facet view reads both sources until then.
- Fisheye ring changes beyond pointing at the skill.

## Acceptance criteria

Slice 0's exact-deploy skill readback and ten-item cold-agent trial below
are pending; local validation/rendering tests do not establish live acceptance.
Only slice 4 remains planned.

0. `get(kind='skill', id='precis-classify-help')` serves; a cold agent
   given only the skill classifies 10 seeded AFM items into axis nodes
   with no ancestor double-links and no combined-node mint.
4. An `exclusive` axis rejects a second `instance-of` from one item onto
   two of its siblings; a multi-label axis accepts it.

## Target + blast radius

Slice 0: `src/precis/data/skills/precis-classify-help.md`, pointers in
`precis-taxon-help.md` and `precis-fisheye-help.md`, boundary-key validation
in `taxonomy/nodes.py` and rendering in `handlers/taxon.py`. Slice 4:
`_link_tag_ops.py::check_relation_constraints` (exclusive check) and the
axis taxon nodes' meta. The only new refusal is at `instance-of` link.

## Open questions / decisions log

- **[decided 2026-10-05, Reto]** File as one item plus a gardener
  amendment; rank as high as practical, slice 0 as an early authoring
  guide.
- **[open]** Calibrate the shipped nearest-sibling cutoff
  (`handlers/taxon.py::SIBLING_MAX_DISTANCE` 0.20 on embedding distance;
  `SIBLING_MIN_OVERLAP` 0.7 lexical fallback, definitions only) together
  with term-taxonomy's `DEDUP_MAX_DISTANCE` follow-up, against hand-judged
  sibling pairs. As shipped the embedding leg never fires on its own: put
  and edit run the all-taxa dedup (0.25) first, which catches any sibling
  within 0.20, so a sibling paraphrase gets the generic "existing node"
  refusal. Decide whether dedup skips siblings (leaving them to this leg)
  or the embedding leg goes.
- **[open]** Whether slice 4's exclusive check refuses or warns on
  legacy links that already violate it.

## Sources

- Liu, Lin, Hewitt, Paranjape, Bevilacqua, Petroni, Liang (2024). Lost in
  the Middle: How Language Models Use Long Contexts. TACL 12:157–173.
  doi:10.1162/tacl_a_00638 — precis paper 468137.
- Yee, Swearingen, Li, Hearst (2003). Faceted metadata for image search
  and browsing. CHI '03, 401–408. doi:10.1145/642611.642681 — precis
  paper 468138.

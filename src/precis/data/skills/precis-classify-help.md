---
id: precis-classify-help
title: precis — classify into the taxonomy (writer and reader rules)
summary: how to attach a ref to taxon nodes, how to write a taxon so its siblings do not overlap, and how to walk a subtree
answers:
  - which taxon node should I attach this paper or finding to?
  - should I link a ref to a node and to its parent too?
  - should I mint a new taxon for this combination of terms?
  - how do I write a taxon definition so it does not overlap its siblings?
  - where do boundary examples and near-misses go?
  - nothing fits — should I pick the closest node?
  - how many children should a taxon node have?
  - how do I find the refs attached to a taxon node?
applies-to: put/link/search/get (kind='taxon', rel='instance-of')
status: active
tags: [workflow]
kinds: [taxon]
---

# precis-classify-help — classify into the taxonomy

Taxon nodes are concepts. Papers, findings, parts and other refs attach to
them with `rel='instance-of'`; measures attach through `property=` (see
`precis-measure-help`). Concept nodes stay few and sharp; instances are
unbounded. Node mechanics (put, meta keys, paths, `under=`):
`precis-taxon-help`.

## Axes

Each `specialises` edge names an axis (`meta={"axis": ...}`), and each axis
answers one question. Any string is accepted today; use exactly these:

| axis | question it answers |
|---|---|
| `method` | how was it done? (DFT, MD, tapping-mode imaging, a calibration correction) |
| `material` | what is it made of? |
| `system` | what object or instrument part? (tip, cantilever, nanotube) |
| `regime` | under which conditions? |
| `quantity` | what is measured or computed? (resonance frequency) |
| `scale` | at what size or time scale? |

Every child on one axis answers that axis's question. A child that answers
a different question goes on that other axis.

## Attach a ref (writer)

`target=` takes the node's id when the source is not a taxon; resolve a
path first:

```python
get(kind="taxon", id="measurand/faradaic-efficiency")      # → tn465823
link(kind="finding", id=4711, target="taxon:465823", rel="instance-of")
```

- One `instance-of` per axis that applies, each to the **most specific**
  node that fits; never also to that node's ancestor.
- Two nodes on one axis only when the ref covers both (a paper comparing
  DFT and MD). A ref you cannot place between two siblings gets their
  parent, not both.
- Read a candidate's `excludes:` lines (`get(kind="taxon", id=...)`)
  before choosing it; each near-miss names the node it belongs to.
- No node fits: the axis's `other` child if it has one, else the parent.
  Never the nearest wrong sibling.

## Mint a node (writer)

Mint only for a concept that recurs. Never:

- a node to hold one ref;
- an intersection of axes ("DFT simulation of AFM tips") — attach the ref
  to the method node and the system node instead;
- a synonym of an existing node — report it (below).

Search the parent's children first:

```python
search(kind="taxon", under="tn812", depth=1)               # existing siblings
search(kind="taxon", q="adjusting a measurement for the probe's own resonance")
```

Definition = genus + difference: what it is, and what separates it from
its nearest sibling. Boundary examples go in `includes` / `excludes`
(shown on `get`, not searched), never in the definition. They, and
`aliases`, are set at put and cannot be added later:

```python
put(kind="taxon",
    text="cantilever resonance correction — a correction to an AFM measurement "
         "for the cantilever's own resonance; it corrects the instrument, not the sample",
    meta={"includes": ["spring-constant calibration from the thermal resonance peak",
                       "higher-eigenmode correction in bimodal AFM"],
          "excludes": ["simulating tip-sample forces → tn<dft tip simulation id>",
                       "resonance of the sample itself → tn<sample resonance id>"]},
    link="taxon:812", rel="specialises")   # put(link=) takes the id, not a path
link(kind="taxon", id=<new tn>, target="taxon:812", rel="specialises",
     meta={"axis": "method"})              # name the axis on the edge
```

A parent with a start node above it may require keys: under `measurand`,
`dimension_kind` is required or the put is refused.

## Siblings that overlap

Siblings are bounded by how clearly they differ, not by a count. Two
signs a parent needs repair:

- two children whose definitions could swap, or refs you keep placing in
  either;
- children answering different questions.

Propose the repair; do not re-link someone else's subtree:

```python
put(kind="todo",
    text="sharpen tn<a>/tn<b>: <the refs that fit both, and why>",   # or merge / re-axis / re-parent
    tags=["waiting-for:reto"])
```

## Walk a subtree (reader)

```python
search(kind="taxon", under="measurand", depth=1)                 # children
search(kind="taxon", under="measurand", axis="quantity", depth=1)  # one axis
get(kind="taxon", id="tn465823", view="path")                    # chain up to the root
get(kind="taxon", id="tn465823", view="links")                   # refs attached to it
```

- Narrow one axis at a time down to the most specific node, then read its
  links.
- Cite the path you narrowed by (`measurand` → `faradaic-efficiency`) so
  the next reader can repeat it.

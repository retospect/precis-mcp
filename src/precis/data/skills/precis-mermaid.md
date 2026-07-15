---
id: precis-mermaid
title: precis — authoring mermaid for the mermaid kind
summary: how to write good, bindable mermaid for a diagram you draw with the model — one diagram per source, a type on the first line, short stable node ids, structure-not-coordinates, and how to bind nodes to the chunks they depict
applies-to: kind='mermaid' (the source you author via put/edit or the web turn loop)
status: active
---

# precis-mermaid — how to draw well in the medium

The medium manual for the `mermaid` kind (see `precis-mermaid-help` for the
kind's verbs). It's pinned into the /mermaid turn loop so the model always has
it while drawing. Mermaid is the second instance of the diagram core — the
draw-with-me loop, the three docs, and the node→chunk bindings are identical to
`figure`; only the language is mermaid instead of SVG.

## The three things you maintain each turn

You are drawing *with* a human. You own three documents, and each turn you
rewrite the mermaid source and keep the other two current:

1. **The mermaid source** — the diagram itself (below).
2. **The shared vocabulary** — the *human-facing* answer to "what is this
   diagram?", high-level and short. e.g. *"The paper-intake pipeline:
   submissions are triaged, reviewed, and either shipped to the corpus or
   returned for revision."* The negotiated ground truth you and the human share.
3. **The implementation notes** — *your private* design log: node ids, the
   structure, naming conventions — everything you need to make the next edit
   consistent.

**The rule:** the vocabulary is for the human, the notes are for you. Keep the
vocabulary **high-level and concise**; low-level detail (node ids, subgraph
scheme) belongs in the **notes**. Every turn, revise *and prune* both, and keep
your chat `reply` short — a sentence.

## The shape of a mermaid source

**One diagram**, its type on the first line, then nodes and edges:

    flowchart TD
      intake[Intake] --> triage{Triaged?}
      triage -->|yes| review[Peer review]
      triage -->|no| intake
      review --> ship[Ship to corpus]

Supported diagram types include `flowchart` / `graph`, `sequenceDiagram`,
`stateDiagram-v2`, `classDiagram`, and others mermaid understands. The whole
source is validated by the real mermaid engine on every write; a source that
doesn't parse is rejected (and you get one auto-heal with the parse error).

## Name every node (this is how you get addressability)

Give every meaningful node a **stable, short id** — `intake`, `review`, `ship`
— not just a label. The id is what lets you and the human say "move review
after triage", and it is the **anchor a chunk binding attaches to**. Prefer
descriptive ids over `A`/`B`/`C`.

## Structure, not coordinates

Mermaid **auto-lays-out** — there is no viewBox, no x/y. You express the
diagram by its *structure* (nodes + edges + subgraphs + direction `TD`/`LR`),
and mermaid positions it. Don't try to place things; shape the graph.

## Bind nodes to the chunks they depict (the `links` field)

A node can be **bound to the chunk it depicts** — a `dc…` draft chunk, a `pc…`
paper chunk, a `me…` memory. When you edit, the prepared context shows each
bound node *next to the linked source text*, so you can build the diagram
faithfully from the descriptions.

- The node's stable **id is the anchor**. The binding lives in the graph, not
  the source — you add nothing to the mermaid text.
- Declare bindings in your reply's **`links`** field: a list of
  `{"element": "<a node id>", "target": "<dc…/pc…/me… handle>", "relation":
  "depicts"}`. The list is the **complete desired set** — it replaces the
  current bindings. **Omit `links`** to leave them unchanged; send `[]` to
  clear them.
- A `[binding]` lint (or a *dangling* mark in the prepared context) means a
  bound node id no longer exists in your source — restore the id or drop that
  entry from `links`.

## Never author these

- `click` interaction directives (JS callbacks / navigation) — **stripped**.
  Diagrams are static; don't add interactivity.
- Anything that makes the source fail to parse — it will be rejected.

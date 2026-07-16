---
id: precis-mermaid-help
title: precis — the mermaid diagram kind
summary: mermaid diagrams you draw with the model — put/get/edit/delete/link, the three model-owned docs, node→chunk bindings, the /mermaid web editor, and the pure-Python mermaidx render/validate/export
applies-to: kind='mermaid'
status: active
---

# precis-mermaid-help — the mermaid kind

A `mermaid` is a diagram you draw **with** the model (flowchart, sequence,
state, class, …). It is the second instance of the shared diagram core beside
`figure`: the same draw-with-me turn loop, the same node→chunk bindings — only
the language is mermaid text instead of SVG. Never exported
(`corpus_role='none'`); addressed by `mm<ref>` / `mn<chunk>`. A first-class kind
(the `[mermaid]` extra provides the render/validate engine). Authoring craft:
`precis-mermaid`.

## The documents

- **source** — the mermaid text (a `mermaid_node` chunk, `mn<id>`; not embedded).
- **shared vocabulary** — high-level, human-facing "what this diagram is" (a
  `mermaid_vocab` chunk; embedded + searchable).
- **implementation notes** — the model's private design log (node ids,
  structure; a `mermaid_notes` chunk; not embedded).
- chat turns persist as `mermaid_turn` chunks (resumable, searchable).

## Verbs

- `put(kind='mermaid', id='<slug>', title='…', project=<todo>, text='<source>',
  vocab='…')` — create. Source defaults to a starter flowchart.
- `get(kind='mermaid', id=None)` lists; `id='<slug>'` renders (source + vocab +
  notes + `## Bindings` + lints); `id='mn<id>'` reads the source node.
- `edit(kind='mermaid', id='<slug>', text=… | vocab=… | notes=…)` — set the
  source, the shared vocabulary, or the notes.
- `delete(kind='mermaid', id='<slug>')` — soft-retire.
- `link(kind='mermaid', id='<slug>', element='<node id>', target='<dc…/pc…/me…>')`
  — bind a node to the chunk it depicts (`mode='remove'` unbinds). Also
  `rel='parent'` for folder placement.

## Bindings (ADR 0057)

A node (by its stable id) binds to the chunk it depicts via a chunk-level
`depicts` link (the node id lives in the link's meta, not the source) — so the
diagram joins the knowledge graph and the model edits with the linked sources
in hand. Drift (a bound id no longer in the source) is caught by a `[binding]`
lint. In the /mermaid turn loop the model sees the prepared context (each node
+ topology + the linked chunk body) and edits bindings via the reply's `links`
field. See `precis-mermaid`.

## Validation, render & export — pure-Python (mermaidx)

Validation, SVG render, and PNG/PDF export all go through **mermaidx** — the
real mermaid.js in an embedded QuickJS engine + a Rust rasterizer, so there is
**no Node, no Chromium, no container**. Every write is validated (an invalid
source is rejected with the mermaid parse error, one auto-heal in the turn
loop). Node extraction for bindings is a pure source scan, so bindings work
even where the engine isn't installed.

## Drawing with the model (the /mermaid web canvas)

The interactive draw-with-me loop is the **web** editor (`/mermaid/<slug>`): the
rendered diagram on the left, the shared vocabulary + chat on the right. Each
turn the model sees the current source, the lints, the vocabulary, the prepared
context, and your message, and rewrites the whole source (a broken reply
auto-heals once, else the good source is kept). From MCP you drive the same data
with `put`/`edit`.

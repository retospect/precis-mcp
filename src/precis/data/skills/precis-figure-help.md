---
id: precis-figure-help
title: precis — the figure kind (interactive SVG canvas you draw with the model)
summary: author an SVG drawing as a slug-addressed chunk-tree, edit it by whole-source rewrite, and draw *with* the model in the /figure web canvas (two shared documents — the SVG source and a shared vocabulary — plus compile + out-of-bounds lints); never exported, browser-rendered
applies-to: get/put/edit/delete/link (kind='figure')
status: active
---

# precis-figure-help — draw SVG figures *with* the model

A `figure` is an interactive **SVG canvas**. It rides the same chunk-tree
substrate as `draft`/`plan` but is a distinct kind (`corpus_role='none'` —
**never exported** as a deliverable; its rendered raster is a later slice).
Slug-addressed; the ref handle is `fg<id>`, the SVG source node is `fn<id>`.

A figure is **three model-owned documents**:

- **the SVG source** — one `figure_node` chunk holding the whole
  `<svg>…</svg>`. Name elements with stable `id=` and `<title>` so you can
  talk about "the left eye". (Raw markup isn't embedded/searched.)
- **the shared vocabulary** — one `figure_vocab` chunk: the *human-facing*,
  high-level ground truth ("green circles are foos", "a friendly round
  mascot"). Embedded + searchable; what keeps a long session coherent.
- **the implementation notes** — one `figure_notes` chunk: the model's
  *private* design log (element ids, structure, conventions). Fed to the
  model every turn but not embedded and hidden from the human by default —
  it's what makes edits consistent without cluttering the shared vocabulary.

Vocab and notes are born **empty** (the "what this doc is for" text is
instruction, kept in the prompt/`precis-figure-svg` skill, never stored as
content); the model fills them as it draws. Chat turns persist as
`figure_turn` chunks, so a session is resumable and searchable.

## Call sequences

Create a figure (optionally under a project todo, optionally seeded):

    put(kind='figure', id='mascot', title='Mascot')
    put(kind='figure', id='mascot', title='Mascot', project=<todo-id>,
        viewbox='0 0 256 256')
    put(kind='figure', id='mascot', text='<svg …>…</svg>', vocab='green = foo')

Read it (assembled SVG + shared vocabulary + `fn<id>` handle + lints):

    get(kind='figure', id='mascot')
    get(kind='figure')            # list all figures
    get(kind='figure', id='fn42') # one source node verbatim

Edit — three independent axes:

    edit(kind='figure', id='mascot', text='<svg …>…</svg>')  # replace source
    edit(kind='figure', id='mascot', vocab='green circles are foos')
    edit(kind='figure', id='mascot', viewbox='0 0 512 384')  # the canvas frame

Retire it:

    delete(kind='figure', id='mascot')

Place it in a folder (ADR 0045): `link(kind='figure', id='mascot',
target='folder:7', rel='parent')`.

## The two mechanical lints (everything else is your job)

Every source write is checked for exactly two things — both pure geometry:

1. **compile** — does the SVG parse as XML, with an `<svg>` root?
2. **out-of-bounds** — does any measurable shape (rect/circle/ellipse/
   line/polyline/polygon) spill past the `viewBox`? (Paths, text and groups
   aren't bounds-checked.)

There is **no** convention checker. "Green circles are foos", symmetry,
palette — those live in the shared vocabulary and are honoured by *you*
reading it each turn, not by a linter.

## Bind elements to the chunks they depict (ADR 0057)

An element (by its stable `id=`) can be **bound to the chunk it depicts** — a
`dc…` draft chunk, a `pc…` paper chunk, a `me…` memory — so the diagram joins
the knowledge graph and the model edits with the linked source in hand.

- `link(kind='figure', id='<slug>', element='<id>', target='<dc…/pc…/me…>')`
  binds an element; `mode='remove'` unbinds it. The binding is a chunk-level
  `depicts` link (the element id lives in the link's meta, **not** in the
  SVG) — reverse-queryable, and drift is caught by a `[binding]` lint when an
  element id no longer exists in the source.
- `get(kind='figure', id='<slug>')` lists the bindings (`## Bindings`).
- In the /figure turn loop the model both **sees** the prepared context
  (each element + its geometry + the linked chunk body) and **edits** the
  bindings via the reply's `links` field. See `precis-figure-svg`.

## Safety (the sanitizer will strip these — don't author them)

`<script>`, `<foreignObject>`, `on*` event handlers, and any external or
`data:` `href`/`xlink:href` are **stripped** on every write (the canvas is
rendered into the browser as an `<img>`, which is already script-safe; the
strip is defense-in-depth). Local `#fragment` refs (gradients, `<use>`)
survive. Name things with `id=`/`<title>`, not XML comments (comments are
dropped on the sanitize round-trip).

## Drawing with the model (the /figure web canvas)

The interactive draw-with-me loop is the **web** editor (`/figure/<slug>`):
a canvas on the left (SVG rendered as an `<img>` + a coordinate grid), the
shared vocabulary + a chat on the right. Each turn the model sees the current
source, the lints, the vocabulary, and your message, and rewrites the whole
SVG (a broken reply auto-heals once, else the good source is kept). That loop
is a web affordance, not an MCP verb — from MCP you drive the same data with
`put`/`edit`. See `precis-figure-svg` for how to author clean SVG.

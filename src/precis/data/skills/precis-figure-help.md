---
id: precis-figure-help
family: drafting
title: precis — the figure kind (interactive SVG canvas you draw with the model)
summary: draw and edit an SVG canvas with the model, read individual elements by handle, and export a PNG or embed it in a draft
answers:
  - how do I create and edit an SVG figure with the model?
  - how do I bind a figure element to the chunk it depicts?
  - what SVG features get stripped by the sanitizer?
applies-to: get/put/edit/delete/link (kind='figure')
tags: orientation, drafting, design
kinds: figure
status: active
---

# precis-figure-help — draw SVG figures *with* the model

A `figure` is an interactive **SVG canvas**. It rides the same chunk-tree
substrate as `draft`/`plan`. The figure ref has `corpus_role='none'`.
Its PNG can be downloaded or included in a draft export. The figure has
a slug and an `fg<id>` handle, and its source chunk has an `fn<id>` handle.

A figure is **three model-owned documents**:

- **the SVG source** — one `figure_node` chunk holding the whole
  `<svg>…</svg>`. Name elements with stable `id=` and `<title>` so you can
  talk about "the left eye". Each top-level element has a child
  `figure_element` chunk with its own `fn<id>`. Named elements keep that
  handle across edits and reorderings. Anonymous elements are tracked by
  position among anonymous siblings of the same tag. Edit the full SVG to
  change the drawing. Raw SVG markup stays out of search indexing.
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

## A figure in a paper or a draft?
## Not the SVG canvas — a paper's figure / a draft's image

The name collides with two neighbours. `figure` here is the **drawable
SVG canvas kind**. A figure *inside a held paper* is a caption + legend
block (`get(id='pc…')`, no image binary served) — see
[[precis-paper-help]]. A figure or image inside a *draft* — placement,
provenance, data tables — is [[precis-draft-rich-content-help]].

## Call sequences

Create a figure (optionally under a project todo, optionally seeded):

    put(kind='figure', id='mascot', title='Mascot')
    put(kind='figure', id='mascot', title='Mascot', project=<todo-id>,
        viewbox='0 0 256 256')
    put(kind='figure', id='mascot', text='<svg …>…</svg>', vocab='green = foo')

Read it (a bounded lint preview + assembled SVG first, then shared
vocabulary / notes / bindings — each doc section header names its
`fn<id>` handle):

    get(kind='figure', id='mascot')
    get(kind='figure')            # list all figures
    get(kind='figure', id='fn42') # one node — over the cap it pages;
                                # follow `rest:` with args={'offset': N}

`view='links'|'log'|'raw'|'lints'` are the ref-inspection views —
`'lints'` pages the full finding list by `args={'offset': N}` (a node
handle takes no `view=` — pass the owning slug).

Edit — three independent axes:

    edit(kind='figure', id='mascot', text='<svg …>…</svg>')  # replace source
    edit(kind='figure', id='mascot', vocab='green circles are foos')
    edit(kind='figure', id='mascot', viewbox='0 0 512 384')  # the canvas frame

Retire it:

    delete(kind='figure', id='mascot')

Place it in a folder (ADR 0045): `link(kind='figure', id='mascot',
target='folder:7', rel='parent')`.

## Export a PNG or embed the figure in a draft

Download PNG from the figure editor, or queue a background render:

```python
put(kind='job', job_type='figure_render', parent_id=123)
```

Use the figure's ref ID as `parent_id`. The job stores a PNG on the source
chunk and reports its SHA-256 and dimensions.

To attach an existing canvas, link it to a draft figure chunk at the
desired caption and position:

```python
link(kind='figure', id='mascot', rel='figure-in', target='dc42')
```

The draft reader shows the SVG. LaTeX and Word use the current PNG and
render it on demand when the source has changed. PNG export captures the
static SVG drawing.

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

## See also

- [[precis-figure-svg]] — authoring clean SVG for the canvas
- [[precis-figure-animate]] — animated figures
- [[precis-paper-help]] — a figure inside a held paper (caption + legend)
- [[precis-draft-rich-content-help]] — figures, images and data tables in a draft

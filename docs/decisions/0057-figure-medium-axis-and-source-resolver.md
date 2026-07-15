# 0057 — Figure medium axis + the source resolver (blob / canvas / graph)

* Status: **Proposed** (design-of-record; build sequenced in §7, not yet implemented)
* Date: 2026-07-15
* Extends: [0034 — figure assets, data supplements, permission provenance](0034-figure-assets-and-permission-provenance.md) · [0035 — computed chunks: payload + recipe](0035-computed-chunks-recipes-and-the-recompute-boundary.md)
* Continues: [0033 — draft chunks as an editable document](0033-draft-chunks-editable-document.md)
* Related: the interactive **`figure` kind** — `src/precis/handlers/figure.py` + `src/precis/figure/{svg,turn}.py`, migrations `0057_figure_kind.sql`/`0058` (a *migration* number, unrelated to this ADR number)

## Context

Two things collided since 0034 was written.

1. **A draft figure is no longer only a pasted bitmap.** 0034 modelled a
   figure chunk as *"a caption + image bytes in `chunk_blobs`"*, with an
   `origin` axis (original / own_graph / third_party) driving clearance. 0035
   added the *graph* case (a render recipe over data). And since then the
   interactive **`figure` kind** shipped — a slug-addressed ref that stores an
   **SVG source** (`figure_node` chunk `fn<id>`) and is drawn *with the model*
   in the `/figure` editor. So a draft figure can now legitimately be: a raster
   bitmap, a static SVG, **one of our live SVG canvases**, or a data-driven
   graph — and soon a Mermaid/TikZ diagram.

2. **The reader assumes every figure is a blob.** `_row.html.j2` emits, for
   *every* `chunk_kind='figure'`, an unconditional
   `<img src="/drafts/blob/{handle}">`. A figure with no blob (a caption-only
   placeholder, or a future canvas-backed figure) 404s → the browser's
   broken-image glyph. And `figure_clearance.figure_status()` marks anything
   that isn't `third_party` as **cleared to ship** — so an *empty* figure reads
   as "all good, ship it". See the motivating bug in §8 (draft **Deck Hook**:
   5 caption-only figures rendering as broken images while the reader cheerily
   reports *"✓ all 5 figure(s) cleared to ship"*).

The root cause is a **conflation of two independent questions**:

* **Do we have the right to publish it?** — a *rights/provenance* question.
* **Where do the pixels come from?** — a *production/medium* question.

0034 fused them into one `origin` enum. The moment "ours ⇒ SVG canvas" (or
"SVG ⇒ canvas", or "a figure ⇒ a blob"), we've cornered ourselves. This ADR
splits them into two orthogonal axes and puts a single resolver seam between
them and the reader / export / clearance, so new figure media (Mermaid today,
a live `structure`/`cad` render tomorrow) extend without touching any consumer.

### What already exists to build on — this needs **no migration**

| Mechanism | Where | Reuse |
|---|---|---|
| Figure chunk + `chunk_blobs` + `meta.figure.origin`/`permission` | ADR 0034 | the `blob` medium, unchanged |
| Graph recipe (`render=`/`plots=`, `set_render_recipe`, `own_graph`) | ADR 0035, `handlers/draft.py`, `plots`/`plotted-by` relations | the `graph` medium, already built |
| Interactive SVG canvas (`kind='figure'`, `figure_node` `fn<id>`, script-safe `<img>` render, `sanitize_svg`) | `handlers/figure.py`, `precis/figure/{svg,turn}.py` | the `canvas` medium |
| **Chunk-level link endpoints** (`links.src_chunk_id` / `dst_chunk_id`) | `links` table | a draft figure *chunk* → a `figure` *ref*, first-class |
| **`has-figure` / `figure-of` relations** (already in the vocabulary) | `relations` table | the canvas pointer; `plots`/`plotted-by` for graph |
| `meta.render` on a figure ref (currently only `"svg"`) | `handlers/figure.py:164` | the **language** slot (svg / mermaid / tikz / …) |
| Clearance rule, single-sourced | `utils/figure_clearance.py` | becomes `origin × medium`-aware |

Every primitive is present: the `links` table already carries chunk endpoints,
`has-figure`/`figure-of` are already relations, the `figure` kind already
serves a sanitized SVG, and `meta.render` is already a language discriminator.
So this ADR is a **convention + a resolver seam + reader/clearance rework** —
**no new table, no new relation, no new migration**.

## Decision

### 1 — Two orthogonal axes

A draft figure chunk (`chunk_kind='figure'`) is described by two independent
axes. Neither implies the other.

**Axis A — origin (rights).** Unchanged from 0034: `original` · `own_graph` ·
`third_party`. Drives **clearance and only clearance**.

**Axis B — medium (production).** A new discriminator `meta.figure.medium`
naming *how the pixels are produced*, which is *what the reader/editor/export
do*:

| `medium` | contract | pixels from | pointer | editor |
|---|---|---|---|---|
| `blob` | opaque bytes | `chunk_blobs` | the blob row | re-upload |
| `canvas` | owned text → sanitized vector, interactive | `has-figure` link → `kind='figure'` SVG | a **link** | `/figure` |
| `graph` | data + recipe → render (ships a data supplement) | recipe render | `plots` links + `set_render_recipe` | recipe box |

They are genuinely independent: a `third_party` figure can be a `blob` (a
scanned SVG) *or* even a `graph` we render from their data; an *ours* figure
can be a `blob` (a PNG we exported), a `canvas`, or a `graph`.

### 2 — Medium is a **contract**, not a **format**. Formats plug in *under* a medium.

The number of `medium` values equals the number of distinct *render/edit
contracts*, and it stays small (three). A new diagram *format* — **Mermaid**,
TikZ, Graphviz-dot, PlantUML — is **not** a new medium: they all share the
`canvas` contract (*an owned text source, authored in a language, compiled to a
sanitized vector, edited in a text/model loop*). They plug in as a **language**
under `canvas`, reusing the figure kind's existing `meta.render` slot:

```
medium = canvas
  meta.render = "svg"      → hand/model-authored SVG (today)
  meta.render = "mermaid"  → Mermaid DSL, compiled to sanitized SVG
  meta.render = "tikz"     → TikZ, compiled to SVG/PDF
  meta.render = "dot"      → Graphviz, compiled to SVG
```

The draw-with-model turn loop authors the source in the chosen language; the
render step compiles it to SVG; everything downstream (script-safe `<img>`,
export, `clearance = ours`) is untouched. **Adding Mermaid touches only the
canvas compiler and the `/figure` editor — zero change to the medium enum, the
resolver, the reader, export, or clearance.** That is the anti-corner property:
*formats* extend along the language dimension, *contracts* extend along the
medium dimension, and the two never collide.

### 3 — The resolver seam (`FigureSource`)

One resolver dispatches on `meta.figure.medium` and gives the reader / export /
clearance a uniform contract. Adding a medium = one adapter; adding a language
= one compiler behind the `canvas` adapter. No consumer changes.

```python
resolve_figure_source(chunk, store) -> FigureSource
    .render_inline()  -> RenderSpec     # inline-sanitized-SVG | <img src=blob_url> | Placeholder
    .export_asset()   -> ExportAsset     # vector | raster bytes for the PDF/docx
    .clearance()      -> (ok: bool, reason: str)     # origin × medium (see §5)
    .edit_target()    -> EditorTarget    # upload-form | /figure(<slug>) | recipe-box | "create canvas"
```

`RenderSpec` is a small tagged union so the template stops assuming a blob:
`inline_svg(markup)` (already-sanitized, served script-safe), `image(url, mime)`,
or `placeholder(kind, cta)` — the last kills the broken-image glyph for a
figure that has no asset yet.

### 4 — Pointer discipline: only `blob` owns bytes

The figure chunk is *"a caption + a typed pointer"*, never *"a caption + a
blob"*. That is what keeps it out of the corner.

* `blob` — bytes in `chunk_blobs` (the chunk owns them).
* `canvas` — a **`has-figure` link** (chunk → `figure` ref). First-class graph
  edge: cascades on delete, shows in lineage, backlink-queryable
  (`figure-of`). *Not* a meta string.
* `graph` — `plots` links to the data chunks + a stored render recipe
  (`set_render_recipe`), exactly as 0035.

So "our figures are SVGs I can edit" = `medium=canvas`, and `/figure` is the
editor — free once wired. "Replicate it adjacent in the document" = render the
linked canvas **inline where the figure sits**, edited in place via the link
(not a forked copy).

### 5 — Clearance becomes `origin × medium`, and honest

`figure_status` moves behind `FigureSource.clearance()` and gains medium
awareness. The rule stays single-sourced (reader banner + export gate):

| medium | origin | cleared? |
|---|---|---|
| *(none — no blob, no canvas, no recipe)* | any | **✗ "no image yet"** |
| `blob` | `third_party` | ✓ iff permission `granted` and not past `expires_at` (0034) |
| `blob` | `original` | ✓ (ours) |
| `canvas` | `original` / `own_graph` | ✓ iff the linked canvas exists and is non-empty (ours — *we made it*) |
| `graph` | `own_graph` | ✓ iff a data supplement is present (0034 §4 / 0035) |

The two bug fixes fall out here: an asset-less figure is **not** cleared (fixes
the "all 5 cleared to ship" lie), and *ours means we made it, it's fine* stays
true for a real canvas/blob (Reto's point).

### 6 — SVG is a security boundary, not a free win

A static SVG (`blob`, `image/svg+xml`) and any `canvas` language that compiles
to SVG (hand-SVG, Mermaid, …) can carry `<script>`/`onload`. Both **must** pass
through the existing `sanitize_svg` on ingest/compile **and** render
script-safe (`<img>`, never a raw inline `<svg>` for untrusted bytes) — the
`figure` kind already does this. The gap to close is the **blob** path: today
`handlers/draft.py:_sniff_mime` doesn't recognise SVG at all (it's text/XML,
not magic-byte sniffable) and never sanitizes it. So blob-SVG support is
*"sniff `<svg`/`<?xml` → `image/svg+xml`, sanitize, serve script-safe"*, not
just a mime-list append.

## Consequences

* **No migration.** New table? No. New relation? No (`has-figure`/`figure-of`
  exist). New column? No — `medium` is a `meta.figure` key. Existing figures
  have no `medium`; derive a default at read time (`recipe/render_pending →
  graph`, else `blob exists → blob`, else `→ none/placeholder`), and stamp it
  going forward. The deck-hook 5 resolve to `none` and render a "drawing
  pending — create in /figure" placeholder.
* **The reader stops assuming a blob.** `_row.html.j2`'s figure branch renders
  a `RenderSpec`, not a hard-coded `<img src=blob>`. One template rework.
* **Extensible by construction.** New format (Mermaid/TikZ) = a compiler under
  `canvas`. New contract (a live `structure`/`cad` render as a figure) = one
  new medium adapter. Neither touches the other, nor any consumer.
* **`origin` keeps its 0034 meaning** — clearance is unchanged for the blob
  cases already in production; the only behavioural change is that an asset-less
  figure is no longer silently cleared.
* Distinct-but-related to the `figure` *kind*: this ADR makes the draft
  `chunk_kind='figure'` able to *point at* a `kind='figure'` canvas. The kind
  keeps owning the SVG/turn-loop; the draft chunk owns the caption + the link.

## Open decisions

1. **Axis-B name.** `medium` vs `render_kind` vs `source`. Avoid `source` — it
   collides with `permission.source_paper`. Leaning `medium`.
2. **Store `medium` vs always derive it.** Storing it (recommended) lets an
   *empty* canvas figure (canvas minted, not yet drawn) render as "pending
   canvas" rather than the ambiguous "none". Derivation stays the back-compat
   default for un-stamped legacy chunks.
3. **Where a canvas lives.** A standalone `figure` slug (reusable, in the figure
   gallery) vs parented under the draft's project (`kind='figure'` takes
   `project=`). Default standalone; project-scope opt-in.

## Phasing (slices)

1. **`canvas` medium, end-to-end** (this branch's target): the `FigureSource`
   seam + `medium` discriminator; reader renders a linked canvas inline (reuse
   the figure kind's script-safe SVG) with **"✎ open in /figure"** and a
   **"create drawing"** CTA on an asset-less figure (mints a `kind='figure'`
   seeded from the caption, wires the `has-figure` link, drops into `/figure`);
   the asset-less → not-cleared clearance fix. Retires the broken-image glyph
   and the false "cleared to ship".
2. **Mermaid under `canvas`** (its own branch off this ADR): `meta.render =
   "mermaid"`, a compile-to-sanitized-SVG step, a Mermaid mode in `/figure`.
   TikZ/dot follow the same shape. **No medium/enum/consumer change.**
3. **`blob`-SVG**: sniff `<svg`/`<?xml` → `image/svg+xml`, sanitize, serve
   script-safe. Closes the static-SVG-as-bitmap gap.
4. **Export**: `FigureSource.export_asset()` — rasterize/vector-embed a canvas
   or compiled diagram into the PDF/docx; the `graph`/`blob` export paths are
   0034 §5 / 0035.

## 8 — Motivating bug (Deck Hook)

Draft **Deck Hook** (`ref 160647`, prod) is a patent-style document. An agent
authored a "Brief Description of the Drawings" section with 5 figure chunks —
FIG. 1–5, captions like *"a perspective view of a deck hook…"* — each with
`meta = {"short": "FIG. 1", "registry": "figures"}`, **no `chunk_blobs` row**,
and **no `meta.figure`**. Under today's reader:

* Each `<img src="/drafts/blob/{handle}">` 404s → the browser's blue
  broken-image box with a question mark. (Bug 1 — the reader assumes a blob.)
* `figure_status()` sees no `third_party` origin → returns cleared → the reader
  prints *"✓ all 5 figure(s) cleared to ship"* over five empty placeholders.
  (Bug 2 — clearance conflated "we have the rights" with "an image exists".)

Under this ADR both vanish: the 5 resolve to `medium=none` → a "drawing
pending — create in /figure" placeholder + **not** cleared; and "create
drawing" turns each into an editable `canvas` (`medium=canvas`,
`meta.render="svg"`) drawn in `/figure`, linked back via `has-figure`.

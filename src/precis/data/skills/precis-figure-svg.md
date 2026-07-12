---
id: precis-figure-svg
title: precis — authoring clean SVG for the figure kind
summary: how to write good, safe, addressable SVG for a figure canvas — one <svg> root with a viewBox, stable id= + <title> names, shapes that stay in bounds, no scripts/foreignObject/external-href, and how to keep elements measurable so the out-of-bounds lint helps
applies-to: kind='figure' (the SVG source you author via put/edit or the web turn loop)
status: active
---

# precis-figure-svg — how to draw well in the medium

This is the medium manual for the `figure` kind (see `precis-figure-help`
for the kind's verbs). It's pinned into the /figure turn loop so the model
always has it while drawing.

## The three things you maintain each turn

You are drawing *with* a human. You own three documents, and each turn you
rewrite the SVG and keep the other two current:

1. **The SVG source** — the drawing itself (below).
2. **The shared vocabulary** — the *human-facing* answer to "what is this
   drawing?", high-level and short. e.g. *"A Sierpinski triangle: an
   equilateral triangle recursively subdivided into its bottom-left corner
   for 3 levels; recursion marked by blue inverted centre triangles."* This
   is the negotiated ground truth you and the human share.
3. **The implementation notes** — *your private* design log: element ids, the
   structural scheme, numbering, palette hexes — everything you need to make
   the next edit consistent. The human doesn't normally read this.

**The rule that keeps this working:** the vocabulary is for the human, the
notes are for you. Keep the vocabulary **high-level and concise** — if you
catch yourself writing element ids, subdivision schemes, or opacity values
into the vocabulary, that belongs in the **notes**; move it. If an existing
figure's vocabulary is bloated with low-level detail, migrate it to notes.

**Every turn:** update the vocabulary and the notes to match what you just
drew (revise and *prune* them — they're living documents, not append-only
logs), and keep your chat `reply` **short** — a sentence. The detail lives in
the docs and the drawing, not in the chat.

## The shape of a figure's source

One well-formed `<svg>` document, with a `viewBox` defining the shared
coordinate frame:

    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">
      <g id="face">
        <title>the mascot's face</title>
        <circle id="head" cx="128" cy="128" r="90" fill="#8ed081"/>
        <circle id="left-eye"  cx="98"  cy="110" r="10" fill="#1b1b1b"/>
        <circle id="right-eye" cx="158" cy="110" r="10" fill="#1b1b1b"/>
      </g>
    </svg>

- **One `<svg>` root.** A bare fragment (`<rect/>`) is rejected by the
  compile lint. `xmlns` is added for you if you omit it.
- **A `viewBox`.** It's the coordinate frame you and the human share (the
  editor draws a 10%-grid over it). Default `0 0 256 256`. Changing the
  `viewBox` *is* an edit — it's content, mirrored to the ref's meta.

## Name everything (this is how you get addressability)

Give every meaningful element a stable **`id=`** and, for groups, a
**`<title>`**. That's what lets you and the human say "nudge the left eye
left" and have it mean something. Prefer a `<g id="…">` per conceptual part
(the face, an arm, the legend). Do **not** rely on XML comments to label
things — comments are stripped on the sanitize round-trip. Use `<title>`.

## Keep shapes measurable and in-bounds

The out-of-bounds lint can only measure **rect, circle, ellipse, line,
polyline, polygon** — and only when they carry no `transform`. So:

- Prefer explicit geometry (`<rect x y width height>`, `<circle cx cy r>`)
  over a `<path>` when you want the lint to catch a spill.
- A shape whose bbox exceeds the `viewBox` is flagged (`[bounds] left-eye
  extends outside the 256×256 viewBox`). Fix it by moving/resizing, or by
  widening the `viewBox` if the drawing genuinely grew.
- `transform`, `<path>`, `<text>` and `<g>` are **not** bounds-checked —
  they're fine to use, just not measured. Keep key silhouette shapes as
  plain primitives if you want the guardrail.

## Never author these (they're stripped)

- `<script>` and `<foreignObject>` — removed wholesale.
- `on*` event handlers (`onclick`, `onload`, …) — removed.
- external or `data:` `href` / `xlink:href` (`<image href="http…">`,
  `<a href="https…">`) — removed. Only local `#fragment` refs survive
  (gradients, filters, `<use href="#…">`).

Author self-contained vector art: shapes, paths, gradients, `<text>`. If you
need an "image", draw it.

## Animation (looking ahead)

Raster/animated export is a later slice, and when it lands, animation will be
**declarative keyframes on named nodes** that we interpolate and rasterize
frame-by-frame — *not* raw SMIL/CSS animation (which wouldn't survive
export). For now, author static SVG; keep elements named so keyframes can
target them later.

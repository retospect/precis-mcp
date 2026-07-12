---
id: precis-figure-animate
title: precis — animating a figure (declarative, browser-native)
summary: opt-in animation for the figure kind — declarative SMIL (<animate>, <animateTransform>, <animateMotion>, <set>) and CSS @keyframes in a <style>, played natively by the browser (no rasterization); static is still the default, reach for this only when the human asks for motion
applies-to: kind='figure' (author motion into the SVG source you edit via put/edit or the web turn loop)
status: active
---

# precis-figure-animate — make a figure move

Additive companion to `precis-figure-svg` (the base medium manual). **Static
is the default** — most figures don't move, and you should not add motion
unless the human asks for it ("make the flame flicker", "spin the gear",
"pulse the dot"). When they do, animate **declaratively** so the *browser*
plays it. Nothing is rasterized: the SVG stays a live vector document.

Everything here survives the figure sanitizer untouched — SMIL elements,
`<style>`, and their timing attributes are not on the stripped list. You
still may not use `<script>`, `on*` handlers, `<foreignObject>`, or
external/`data:` `href` (see `precis-figure-svg`); declarative animation
needs none of those.

## Two ways to animate (pick per case)

**1. SMIL — animate one attribute of one named element.** Best for a
specific shape doing a specific thing. Target the element by wrapping the
animation inside it (or by `href="#id"`).

    <circle id="dot" cx="128" cy="128" r="8" fill="#e11">
      <animate attributeName="r" values="8;14;8" dur="1.2s"
               repeatCount="indefinite"/>
    </circle>

- `<animate>` — a numeric/colour attribute (`r`, `opacity`, `fill`, `cx`…).
- `<animateTransform>` — rotate/scale/translate:
  `<animateTransform attributeName="transform" type="rotate"
   from="0 128 128" to="360 128 128" dur="3s" repeatCount="indefinite"/>`.
- `<animateMotion>` — move a shape along a `<path>` (`<mpath href="#track"/>`).
- `<set>` — a one-shot state change at a `begin=` time.
- Timing knobs: `dur`, `begin` (`0s`, `dot.click`, `other.end+0.5s`),
  `repeatCount` (`indefinite` or a number), `values`/`keyTimes` for
  multi-stop, `fill="freeze"` to hold the final value.

**2. CSS `@keyframes` — reusable motion across many elements.** Put one
`<style>` in the SVG and drive elements by class:

    <style>
      @keyframes flicker { 0%,100% { opacity: 1 }   50% { opacity: .6 } }
      .flame { animation: flicker .8s ease-in-out infinite; transform-box: fill-box;
               transform-origin: center; }
    </style>
    <path class="flame" d="…" fill="#f90"/>

Use `transform-box: fill-box; transform-origin: center` when you rotate/scale
so the element spins about its own centre, not the SVG origin.

## Keep it measurable and in-bounds

The out-of-bounds lint measures a shape's **static** geometry (its authored
`cx`/`r`/`x`… before any animation) — animation doesn't move the checked
bbox, so it won't trip the lint. But *think about the swept area*: an element
that animates or `transform`s outside the `viewBox` will visibly clip at the
canvas edge even though the lint stays quiet. Keep the whole motion path
inside the frame, or widen the `viewBox`.

- Name every animated element with a stable `id=` (and its motion is then
  addressable: "slow the flame", "make the dot pulse wider").
- A `transform`ed/animated shape isn't bounds-checked (documented limitation),
  so keep a key silhouette shape as a plain in-bounds primitive if you want
  the guardrail.

## Discipline

- **Default to static.** Only animate what the human asked to move; leave the
  rest static. A figure where everything wiggles is worse, not better.
- **Loop gently.** Prefer slow, subtle, `indefinite` loops for ambient motion
  (a flicker, a breathe); reserve fast/one-shot motion for a deliberate beat.
- **One mechanism per element.** Don't drive the same attribute from both a
  SMIL `<animate>` and a CSS rule — they fight.
- Edit animation the same way as everything else: rewrite the whole `<svg>`.

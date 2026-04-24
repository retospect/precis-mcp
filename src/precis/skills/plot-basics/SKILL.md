---
name: plot-basics
description: >
  Render declarative JSON plot specs to PNG / SVG / WebP / PDF via
  matplotlib.  Use for line, scatter, bar, hist, and errorbar plots.
  Output is an inline data URL (PNG default, SVG / WebP opt-in) or a
  file on disk (/export).  Safe, deterministic, no code execution —
  the spec is pure data.  Free, local.
user-invocable: true
argument-hint: [spec-json]
allowed-tools: [put, get]
applies-to: [plot]
kind-onboarding: plot
tags: [plotting, visualization, matplotlib, figures, charts]
---

## When to use

- Visualize numerical data: measurements, fits, comparisons, distributions.
- Generate figures for reviews (sanity-check a trend before citing).
- Produce LaTeX-ready vector figures (SVG / PDF via `/export`).
- Quick inline PNG for a chat or doc.

**Do not use** for non-plot computations — those belong in `calc:`
(exact / symbolic) or `math:` (Wolfram, paid).  Do not use for plots
outside the shipped vocabulary (violin, heatmap, contour, 3D) — those
either wait for a future type or fall back to a full Python runner.

## The minimum viable spec

One required field, `type`:

```
{"type": "line", "x": [1, 2, 3, 4], "y": [1, 4, 9, 16]}
```

That's enough for a blue line with auto axes, no title, no legend.
Everything else is an optional override.

## Invocation

All plots go through `put()` with `mode='render'` — the handler is
stateless, nothing is stored.  The `id` encodes the output format and
whether to write a file:

```
put(id='plot:',                    text='<spec>', mode='render')  # inline PNG
put(id='plot:/svg',                text='<spec>', mode='render')  # inline SVG
put(id='plot:/webp',               text='<spec>', mode='render')  # inline WebP
put(id='plot:/export',             text='<spec>', mode='render')  # → ./figures/plot-<hash>.png
put(id='plot:/export/foo.png',     text='<spec>', mode='render')  # → ./figures/foo.png
put(id='plot:/export/foo.svg',     text='<spec>', mode='render')  # → ./figures/foo.svg
put(id='plot:/export/foo.pdf',     text='<spec>', mode='render')  # → ./figures/foo.pdf
put(id='plot:/export/foo.webp',    text='<spec>', mode='render')  # → ./figures/foo.webp
```

PDF is export-only (binary, too bulky for inline).  All exports land
under the caller's `./figures/` directory — absolute paths and `..`
traversal are rejected.

## Common fields (all types)

```
title:    "string"
xlabel:   "string"
ylabel:   "string"
xscale:   "linear" | "log"
yscale:   "linear" | "log"
xlim:     [min, max]
ylim:     [min, max]
grid:     true            # default
legend:   true            # default
figsize:  [width_in, height_in]   # default [6, 4], bounds 1..20
dpi:      100             # default, range 50..600
palette:  "default" | "tab10" | "viridis" | "plasma" | "grayscale" | "colorblind"
annotate: [{"x": 300, "y": 0.5, "text": "kink"}, ...]
hline:    [{"y": 1.0, "label": "baseline", "style": "dashed"}, ...]
vline:    [{"x": 25,  "label": "onset",    "style": "dotted"}, ...]
```

Line styles: `solid | dashed | dotted | dashdot`.

## type: line

Single series (top-level `x`/`y`) or multiple (`series[]`):

```
{
  "type": "line",
  "title": "Rate over time",
  "xlabel": "t (s)", "ylabel": "r (mol/s)",
  "x": [0, 1, 2, 3, 4],
  "y": [0.1, 0.3, 0.5, 0.8, 1.2],
  "label": "run A"
}
```

Multiple:

```
{
  "type": "line",
  "series": [
    {"x": [0,1,2,3], "y": [1,2,4,7], "label": "A", "style": "solid"},
    {"x": [0,1,2,3], "y": [1,1,2,3], "label": "B", "style": "dashed"}
  ]
}
```

Optional `fit` overlay — `linear | log | exp | arrhenius`:

```
{
  "type": "line",
  "x": [1,2,3,4,5], "y": [2.1,3.9,6.1,8.0,9.9],
  "fit": {"kind": "linear", "report": true}
}
```

The response text includes the slope / intercept / R² above the image.

## type: scatter

Same data shape as `line` (single or multi-series), marker defaults to
`o`, same `fit` options.  Useful for Arrhenius plots:

```
{
  "type": "scatter",
  "title": "Arrhenius plot",
  "xlabel": "1000/T (K⁻¹)", "ylabel": "ln(k)",
  "x": [2.10, 2.25, 2.41, 2.58],
  "y": [-12.3, -11.4, -10.6, -9.9],
  "fit": {"kind": "arrhenius"},
  "annotate": [{"x": 2.10, "y": -12.3, "text": "T=476K"}]
}
```

## type: bar

Single series:

```
{
  "type": "bar",
  "title": "Activity by catalyst",
  "labels": ["Fe", "Cu", "Ni"],
  "values": [3.2, 5.1, 2.8]
}
```

Grouped (side-by-side):

```
{
  "type": "bar",
  "labels": ["Fe", "Cu", "Ni"],
  "series": [
    {"label": "2023", "values": [3.2, 5.1, 2.8]},
    {"label": "2024", "values": [3.5, 4.9, 3.1]}
  ]
}
```

Horizontal bars: add `"horizontal": true`.

## type: hist

```
{
  "type": "hist",
  "title": "Pore-size distribution",
  "values": [2.1, 2.3, 2.4, 3.1, 3.5, 3.6, 4.2, 4.2, 5.0],
  "bins": 20
}
```

`bins` accepts an int (number of equal-width bins) or an explicit
list of edges, e.g. `[0, 1, 2, 5, 10]`.

## type: errorbar

```
{
  "type": "errorbar",
  "title": "Measured rate ± σ",
  "x":    [1, 2, 3, 4, 5],
  "y":    [1.1, 2.0, 3.2, 4.1, 4.9],
  "yerr": [0.1, 0.2, 0.15, 0.3, 0.2]
}
```

Both `xerr` and `yerr` are optional; at least one should be supplied
or you may as well use `scatter`.

## What is *not* in the spec

- **No arbitrary matplotlib kwargs pass-through.**  Every visual
  choice maps to a named field — that's the safety argument.  If you
  need `twinx`, subplots, 3D, or custom colormaps, produce multiple
  plots or fall back to a Python runner.
- **No subplots / gridspec.**  One axes per plot.  Stack figures in
  LaTeX if you need side-by-side.
- **No twin axes.**  If you need a secondary y, the plot is probably
  lying — use two panels.
- **No custom per-point colors.**  Override via `palette` only; the
  goal is consistency across a document.

## Limits

- Each numeric array is capped at 50,000 points.
- Inline payload is capped at 2 MB after base64 encoding.  Above that
  the handler refuses and tells you to use `/export`.
- `figsize` is bounded to 1–20 inches per side; `dpi` to 50–600.

## Errors you might see

- `"plot: mode must be 'render'"` — you passed `mode='replace'` or
  similar.  plot is write-only-compute; the mode is always `render`.
- `"plot: invalid JSON — …"` — your spec didn't parse.  Check braces
  and quotes; JSON requires double quotes.
- `"plot: spec invalid at <path> — …"` — pydantic validation failed.
  Unknown fields trigger this (`extra='forbid'`).
- `"plot: series N has mismatched lengths"` — x and y arrays don't
  line up.
- `"plot: rendered image is N KB which exceeds the inline cap"` —
  switch to `/export` or reduce `dpi` / `figsize`.

## Tips

- **Default dpi is 100** — fine for inline previews.  For LaTeX
  figures, export at `dpi: 300` with SVG or PDF (vector formats
  ignore dpi but the field is still accepted).
- **Title / labels absorb LaTeX math** with `$...$` — matplotlib
  parses it natively, e.g. `"ylabel": "$\\Delta G$ (eV)"`.
- **For Arrhenius plots** the `arrhenius` fit kind is just linear but
  the report line is labelled accordingly.  Use it when x is `1/T`
  and y is `ln(k)`.
- **Palettes**: `colorblind` is the Okabe–Ito 8-color set, good for
  accessibility; `viridis` / `plasma` for sequential comparisons.

## See also

- `get(id='calc:<expr>')` — exact arithmetic / symbolic math.  Use
  this for the numbers *behind* a plot.
- `get(id='math:<query>')` — paid Wolfram for natural-language math
  and world-data lookups.
- `get(id='skill:tex-workflow')` — inserting exported figures into
  LaTeX documents.

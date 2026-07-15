# 0057 — Element→chunk binding for diagrams, and a `mermaid` kind

- **Status**: proposed (2026-07-15). Records the *decisions*; the full
  architecture, prepared-context shape, and build order live in
  [`docs/design/diagram-editing-and-chunk-binding.md`](../design/diagram-editing-and-chunk-binding.md).
- **Deciders**: Reto + agent
- **Builds on**:
  - **figure** (migrations [0057_figure_kind]/[0058_figure_notes],
    `src/precis/figure/`) — the draw-with-me turn loop, three-doc model
    (source / shared vocab / private notes), and SVG sanitize/lint this
    generalizes. `figure` becomes an *instance* of the shared diagram core.
  - [ADR 0036 — universal handles](./0036-universal-handles.md) — `dc<id>` /
    `pc<id>` / `me<id>` stable chunk addresses are the binding targets.
  - [ADR 0035 — reactive figure/data edges](./0035-figure-data-reactive-edges.md)
    (`plots` / `plotted-by`) — `depicts` is its element-granular cousin.
  - [ADR 0056 — open relation vocabulary](./0056-chemistry-tool-packs-plugin-route-kind.md)
    — `validate_relation(store=…)` reads the live `relations` table, so a new
    relation is a migration insert, no core literal edit.
  - [ADR 0044 — the derived-job lane](./0044-derived-job-lane.md) — the
    autonomous `diagram_propose` tick (slice 5) rides this lane.

## Context

`figure` is a closed world: an SVG canvas the model draws with the human that
knows nothing about the corpus it depicts. But a figure of a part, or a
mermaid flow of a pipeline, *is about things precis already holds* — a `draft`
part chunk, a `cad` cross-section, a paper method, a memory. The draft/plan
editor already edits a chunk **inside a prepared context** that quotes its
text and lists its linked sources (`planner_prompt._render_anchor_context`).
Diagrams should get the same frame — generalized from one anchor chunk to
every element of the diagram — so the model reasons about the drawing with the
sources in hand, and the diagram joins the knowledge graph.

Separately, mermaid is a common diagram language worth a first-class kind, and
it wants the identical turn loop, bindings, and context — differing only in
source language.

## Decision

1. **Element→chunk binding is hybrid.** The element's stable `id=` in the
   source is the **join key**; a chunk→chunk **link row** is the semantic
   edge, with the element id in `link.meta.element`. `src` = the diagram's
   source chunk, `dst` = the target chunk, `relation = 'depicts'`. This needs
   **zero sanitizer change**, is reverse-queryable via the existing
   `chunk_connections()`, and a **dangling-binding lint** catches id drift.
   Rejected: binding embedded in the markup (invisible to the graph, needs
   sanitizer work); binding in the link table with no source `id=` anchor
   (silent drift).

2. **New relation `depicts` / `depicted-in`**, chunk-level, seeded into
   `relations` by migration — no core edit (open vocab, ADR 0056). The
   element-granular cousin of `plots`/`plotted-by`.

3. **A prepared-context assembler** `render_diagram_context(store, ref)` lists
   every element (id, shape, coords/topology) with its bound chunk (handle,
   relation, title) and inlines the linked chunk bodies. It feeds both the
   turn prompt and the `get()` read verb, so MCP agents and the web editor see
   the same context. Coords are geometry on the SVG side (reusing figure's
   `_shape_bbox`), topology (node + edges) on the mermaid side.

4. **The turn JSON grows a `links` array**, so the model creates/adjusts
   bindings as it draws; bindings are reconciled after the sanitize-gated
   source write, and left untouched when `links` is omitted.

5. **Factor a shared diagram core** (`src/precis/diagram/`) behind a
   `DiagramLang` port (compile / sanitize / lint / elements / render).
   `figure` = the SVG instance (mechanical, parity-guarded refactor);
   `mermaid` = the mermaid instance. Not a copy of figure — both are
   instances.

6. **Mermaid validation/render/export is pure-Python via `mermaidx`** —
   embedded QuickJS runs the real mermaid.js, `resvg-py` rasterizes. No Node,
   no Chromium, no container: `render(src).svg()` validates (raising the real
   mermaid parse error with line/caret) and renders in ~20 ms warm, on the Mac
   turn host and Linux alike (verified 2026-07-15; wheels cover the fleet, no
   compiler). Display reuses figure's `sanitize_svg` on the SVG output.
   Behind a `[mermaid]` extra + lazy import, `PRECIS_MERMAID_ENABLED` default
   OFF — ships dark. **Rejected: `mmdc`/mermaid-cli in a container** on the
   derived-job lane (the keystone "rent the kernel" pattern, but made
   unnecessary — kept as the fallback behind the port), and a hand-written
   Python mermaid grammar (would rot against upstream; we keep only a tiny
   source scan for node/edge *extraction*, never as the validation authority).

## Consequences

- **Positive.** Diagrams become graph citizens; the model edits with sources
  in hand; figure and mermaid share one loop, one sanitizer, one context
  assembler. One pure-Python renderer covers validate + render + export with
  no type/interop seams and no infra.
- **Costs / risks.** A third-party single-maintainer dependency (`mermaidx`,
  renamed once) — isolated behind the extra + the `DiagramLang` port, MIT and
  forkable. Bundled mermaid version tracks `termaid`. Element `id=` drift is
  possible — caught by the dangling-binding lint, not prevented. Only ref-level
  links were exposed before; chunk-level `link=` is new surface to test.
- **Build order** (design doc): (1) binding substrate dark → (2) figure
  bindings + prepared context [stands alone] → (3) factor the core →
  (4) mermaid kind dark → (5) `diagram_propose` tick.

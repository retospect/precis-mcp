# Rich diagram editing with element→chunk binding — residuals

Shipped portion: see the `precis.diagram` and `precis.mermaid` package
docstrings; full design-of-record in git history. Live (un-darked
2026-07-16, first-class kind): the chunk-binding substrate
(`depicts`/`depicted-in`, migration 0064), figure element bindings +
prepared context, the shared `src/precis/diagram/` core, the `mermaid`
kind (migration 0066) with pure-Python `mermaidx`
validation/render/export, the `diagram_propose` tick executor, and the
`precis-mermaid*` skill family.

## Open scope (residuals)

- **Engine gaps:** gantt / pie / sankey / C4 / block don't render —
  the in-process QuickJS engine lacks browser globals (offsetWidth,
  structuredClone, …). Bump `mermaidx` when upstream ships a fuller
  shim, evaluate termaid, or polyfill the cheap globals
  (`precis-mermaid-unsupported` steers models to renderable
  alternatives meanwhile). Owner: `src/precis/mermaid/mermaid.py`.
- **`diagram_propose`:** render richer per-kind seed content (a
  figure's SVG, a cad cross-section) instead of a titled reference.
- **Self-directed drawer:** mermaid L1/L2 auto-context (owning-draft
  reverse resolver + route `document_context_for`; figures get it
  free); the L2 semantic leg — embed instruction entities + rank the
  draft's chunks, not just literal term hits. Owner:
  `src/precis/diagram/doc_context.py`.
- Housekeeping: primary-repo branch `wip/backlog-docs` holds one
  local-only commit (`e5643873`) — ship or drop it.

## Decided constraints (rejected alternatives, condensed)

- `mermaidx` (real mermaid.js in QuickJS) is the validation authority
  everywhere — never a hand-written Python grammar (rots against
  upstream), never browser-round-trip-only (misses the MCP path). A
  container `mmdc` path stays the fallback behind the `DiagramLang`
  port only if `mermaidx` becomes unviable.
- Binding is the **hybrid**: source `id=` is the join key, the
  `links` table is the graph truth, lints catch drift — never
  source-only (invisible to the graph) or table-only (ids drift
  silently).
- "Never persist a broken source" — validation runs on every
  `put`/`edit`/tick, in-process.

# diagram-editing-and-chunk-binding

## Residuals (from OPEN-ITEMS)

- Engine gaps: gantt/pie/sankey/C4/block don't render — the in-process
  QuickJS engine lacks browser globals (offsetWidth, structuredClone, …).
  Bump mermaidx when upstream ships a fuller shim, evaluate termaid, or
  polyfill the cheap globals (`precis-mermaid-unsupported` steers models to
  renderable alternatives meanwhile). Owner `src/precis/mermaid/mermaid.py`.
- diagram_propose: render richer per-kind seed content (a figure's SVG, a cad
  cross-section) instead of a titled reference.
- Self-directed drawer: mermaid L1/L2 auto-context (owning-draft reverse
  resolver + route document_context_for; figures get it free); L2 semantic
  leg — embed instruction entities + rank the draft's chunks, not just
  literal term hits (owner `src/precis/diagram/doc_context.py`).
- Primary-repo branch wip/backlog-docs holds one local-only commit
  (e5643873) — ship or drop it.

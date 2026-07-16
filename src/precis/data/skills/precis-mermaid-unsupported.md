---
id: precis-mermaid-unsupported
title: precis — gantt chart / pie chart / sankey / C4 diagram / block diagram (not yet renderable)
summary: mermaid diagram types the in-process engine cannot render yet — and what to use instead
applies-to: kind='mermaid'
status: active
---
The in-process render engine (mermaidx / QuickJS, no browser DOM) **cannot
render these mermaid types yet** — a write will validate-fail, so do not
reach for them:

- **gantt** (project schedule / timeline with durations) — engine needs
  `offsetWidth`. Instead: a `precis-mermaid-timeline` for a milestone
  chronology, or a draft `table` for a dated schedule with durations.
- **pie** (pie chart / proportions / share-of-total) — engine needs
  `structuredClone`. Instead: a `precis-mermaid-xychart` bar chart, or a
  draft `table` of shares.
- **sankey-beta** (flow / sankey diagram) — unsupported. Instead: a
  `precis-mermaid-flowchart` with labelled edges.
- **C4Context** (C4 architecture diagram) — engine needs `screen`. Instead:
  a `precis-mermaid-flowchart` grouped with subgraphs.
- **block-beta** (block diagram) — unsupported. Instead: a
  `precis-mermaid-flowchart`.

Tracked to fix (engine upgrade / polyfills) in OPEN-ITEMS.md. Everything else
— flowchart, sequence, class, state, ER, journey, quadrant, requirement, git
graph, timeline, xychart, mindmap — renders; see the `precis-mermaid-*`
skills and `precis-mermaid-help`.

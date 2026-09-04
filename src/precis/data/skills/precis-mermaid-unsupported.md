---
id: precis-mermaid-unsupported
title: precis — sankey / block diagram (not yet renderable)
summary: mermaid diagram types the in-process engine cannot render yet — and what to use instead
answers:
  - can precis render a sankey or block diagram?
  - what should I use instead of an unsupported mermaid diagram type?
applies-to: kind='mermaid'
status: active
---
The in-process render engine (mermaidx / QuickJS, no browser DOM) **cannot
render these mermaid types** — a write will validate-fail (`put`/`edit`
raises), so do not reach for them:

- **sankey-beta** (flow / sankey diagram) — unsupported. Instead: a
  `precis-mermaid-flowchart` with labelled edges.
- **block-beta** (block diagram) — unsupported. Instead: a
  `precis-mermaid-flowchart`.

This list shrinks as `mermaidx` gains coverage — as of `mermaidx>=0.9`,
**gantt**, **pie**, and **C4Context** render successfully (they used to be
here too; a stale claim of engine gaps for them was gr311345). If a type not
listed here silently fails, that is a real regression, not an intentional
gap — file it. Tracked in the repo backlog. Everything else — flowchart,
sequence, class, state, ER, journey, quadrant, requirement, git graph,
timeline, xychart, mindmap, gantt, pie, C4Context — renders; see the
`precis-mermaid-*` skills and `precis-mermaid-help`.

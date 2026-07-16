---
id: precis-mermaid-gitgraph
title: precis — git graph / git branching diagram / commit history / branch flow
summary: a mermaid gitGraph — branches, commits, and merges of a git history
applies-to: kind='mermaid'
status: active
---
A **git graph** draws a git history: branches, commits, and merges. Reach for
it when the user says "git graph", "git branching diagram", "branch flow", or
"commit history".

    gitGraph
      commit
      branch develop
      checkout develop
      commit
      checkout main
      merge develop

CRUD, bindings, the /mermaid canvas → `precis-mermaid-help`. Authoring craft →
`precis-mermaid`.

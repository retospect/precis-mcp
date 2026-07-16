---
id: precis-mermaid-flowchart
title: precis — flowchart / flow chart / process diagram / decision tree / org chart / workflow
summary: a mermaid flowchart — boxes and arrows for a process, decision tree, org chart, or workflow
applies-to: kind='mermaid'
status: active
---
A **flowchart** models a process, decision tree, org chart, or workflow as
nodes and directed edges. Reach for it when the user says "flow chart",
"process diagram", "decision tree", "org chart", or "workflow".

    flowchart TD
      A[Start] --> B{Decision?}
      B -->|yes| C[Do the thing]
      B -->|no| D[Stop]

CRUD, bindings, the /mermaid canvas → `precis-mermaid-help`. Authoring craft →
`precis-mermaid`.

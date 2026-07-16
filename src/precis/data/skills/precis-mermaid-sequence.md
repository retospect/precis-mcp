---
id: precis-mermaid-sequence
title: precis — sequence diagram / interaction diagram / message flow / call flow
summary: a mermaid sequence diagram — actors exchanging messages over time
applies-to: kind='mermaid'
status: active
---
A **sequence diagram** shows actors/participants exchanging messages over
time (an interaction, protocol handshake, API call flow). Reach for it when
the user says "sequence diagram", "interaction diagram", "message flow", or
"who calls whom".

    sequenceDiagram
      Alice->>Bob: Request
      Bob-->>Alice: Response

CRUD, bindings, the /mermaid canvas → `precis-mermaid-help`. Authoring craft →
`precis-mermaid`.

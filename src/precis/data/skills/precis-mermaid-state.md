---
id: precis-mermaid-state
title: precis — state diagram / state machine / FSM / lifecycle / status flow
summary: a mermaid state diagram — states and the transitions between them
applies-to: kind='mermaid'
status: active
---
A **state diagram** models a state machine (FSM): the states a thing can be
in and the events that transition between them. Reach for it when the user
says "state machine", "state diagram", "FSM", "lifecycle", or "status flow".

    stateDiagram-v2
      [*] --> Idle
      Idle --> Running: start
      Running --> Idle: stop
      Running --> [*]

CRUD, bindings, the /mermaid canvas → `precis-mermaid-help`. Authoring craft →
`precis-mermaid`.

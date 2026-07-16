---
id: precis-mermaid-requirement
title: precis — requirement diagram / requirements traceability / verification matrix
summary: a mermaid requirement diagram — requirements and what satisfies/verifies them
applies-to: kind='mermaid'
status: active
---
A **requirement diagram** (SysML-style) captures requirements and the
elements that satisfy, verify, or derive from them — a traceability view.
Reach for it when the user says "requirements diagram", "traceability", or
"verification matrix".

    requirementDiagram
      requirement req1 {
        id: 1
        text: the system shall log in under 2s
        risk: high
        verifymethod: test
      }
      element login_test {
        type: simulation
      }
      login_test - verifies -> req1

CRUD, bindings, the /mermaid canvas → `precis-mermaid-help`. Authoring craft →
`precis-mermaid`.

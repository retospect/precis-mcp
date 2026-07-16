---
id: precis-mermaid-er
title: precis — ER diagram / entity-relationship / database schema / data model
summary: a mermaid ER diagram — entities, attributes, and their relationships
applies-to: kind='mermaid'
status: active
---
An **ER diagram** models entities and the relationships between them — the
shape of a database schema or data model, with cardinality. Reach for it
when the user says "ER diagram", "entity-relationship", "database schema", or
"data model".

    erDiagram
      CUSTOMER ||--o{ ORDER : places
      ORDER ||--|{ LINE_ITEM : contains

CRUD, bindings, the /mermaid canvas → `precis-mermaid-help`. Authoring craft →
`precis-mermaid`.

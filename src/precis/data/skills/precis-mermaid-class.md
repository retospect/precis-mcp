---
id: precis-mermaid-class
title: precis — class diagram / UML / object model / type hierarchy / inheritance
summary: a mermaid class diagram — UML classes, fields, methods, and inheritance
applies-to: kind='mermaid'
status: active
---
A **class diagram** is UML: classes with fields and methods, and the
relations between them (inheritance, composition, association). Reach for it
when the user says "class diagram", "UML", "object model", "type hierarchy",
or "inheritance".

    classDiagram
      class Animal {
        +String name
        +eat()
      }
      Animal <|-- Dog

CRUD, bindings, the /mermaid canvas → `precis-mermaid-help`. Authoring craft →
`precis-mermaid`.

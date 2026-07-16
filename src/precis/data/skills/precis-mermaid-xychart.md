---
id: precis-mermaid-xychart
title: precis — xy chart / bar chart / line chart / plot / graph of values
summary: a mermaid xychart — a bar and/or line chart over an x-axis
applies-to: kind='mermaid'
status: active
---
An **xy chart** is a bar and/or line chart over an x-axis — a quantitative
plot of values. Reach for it when the user says "bar chart", "line chart",
"plot", or "graph of numbers". (For proportions/shares — a pie chart — see
`precis-mermaid-unsupported`.)

    xychart-beta
      title "Sales"
      x-axis [jan, feb, mar]
      y-axis "Revenue" 0 --> 100
      bar [30, 50, 80]
      line [20, 40, 70]

CRUD, bindings, the /mermaid canvas → `precis-mermaid-help`. Authoring craft →
`precis-mermaid`.

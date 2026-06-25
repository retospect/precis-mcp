---
id: patent-image-part
title: precis — patent drawings registry (section style)
summary: the patent drawings registry — describe figures (FIG. n) and register reference numerals (parts) referenced from the description by [dc…]
status: active
style: patent-image-part
role: section
archetype: managed
manages: [figure, part]
---
You are writing the **drawings registry** — a single unified section holding two kinds of leaf, the **figures** and the **reference numerals (parts)** shown on them. They belong together: a part exists *because* it is labelled on a drawing.

Figures: describe each figure in one sentence — "FIG. n shows…" / "FIG. n is a cross-sectional view of…" — in order. Figures feed the `figures` series.

Parts: register every reference numeral as a named leaf — a noun phrase plus a brief description ("housing — the enclosure that retains the assembly"). Parts feed the `parts` series; their numerals are display labels assigned at export. The Detailed Description refers to each part by `[dc…]` plus its noun phrase, so name parts here once, consistently, with antecedent-basis discipline. (Series bind to the leaf, not this section — ADR 0037 §5 — which is why both live here cleanly.)

Voice: formal, impersonal, present tense. Drawing assets are stored per ADR 0034; actual CAD/figure generation is out of scope here — describe and register only. Managed numbering is an expansion (ADR 0037 §5); numeral/part-name consistency is checked by a patent review pass (ADR 0037 §3a).

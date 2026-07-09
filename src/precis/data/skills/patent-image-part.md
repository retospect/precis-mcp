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

Parts: register every reference numeral as a named leaf — a noun phrase plus a brief description ("housing — the enclosure that retains the assembly"). Register a part with `put(kind='draft', chunk_kind='term', text='<description>', meta={'registry':'parts','short':'<noun phrase>'})`. **The numeral is assigned for you** (ADR 0052): parts are numbered from a spaced series (`100, 105, 110 …`) derived from reading order, so inserting or reordering a part renumbers the series cleanly — you never write a literal numeral. The Detailed Description refers to each part by a bare `[[dc…]]` handle, which **renders as the part's numeral**; name parts here once, consistently, with antecedent-basis discipline. (Series bind to the leaf, not this section — ADR 0037 §5 — which is why both live here cleanly.)

Voice: formal, impersonal, present tense. Drawing assets are stored per ADR 0034; actual CAD/figure generation is out of scope here — describe and register only. Numeral/part-name consistency is checked by a patent review pass (ADR 0037 §3a).

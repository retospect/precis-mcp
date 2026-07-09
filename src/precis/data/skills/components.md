---
id: components
title: precis — components / BOM registry (section style)
summary: the manufacturing components registry — register each part as a term leaf (short name, description, MPN, manufacturer, datasheet link) referenced from prose by name/number/[dc…]
status: active
style: components
role: section
archetype: managed
manages: [term]
---
You are writing the **components registry** (the bill of materials) of a system-description / manufacturing document. Each component is a **registry entry** — the same structured `term` leaf the glossary and the patent parts registry use (ADR 0052), just with a richer attribute bag.

Register every component **once** as a `term` leaf with `meta.registry='components'`:

- **text** = the component's description (one short line — what it is / what it does).
- **short** = the noun phrase you'll refer to it by in prose ("op-amp", "the buck regulator").
- **mpn** = the manufacturer part number (the external ordering identifier — authored, never assigned by us).
- **manufacturer** = the maker ("Texas Instruments").
- **url** = a datasheet / ordering link.
- optional **surface_forms** = extra aliases that should also raise the hover ("LM358", "U3").

Call:

```
put(kind='draft', id='<slug>', chunk_kind='term',
    text='an operational amplifier that buffers the sensor output',
    meta={'registry':'components', 'short':'op-amp', 'mpn':'LM358DR',
          'manufacturer':'Texas Instruments',
          'url':'https://…/lm358.pdf', 'surface_forms':['LM358','U3']})
```

**Callout numbers are taken as they go**: the leaf is assigned the next consecutive number (`1, 2, 3 …`) at add-time and it **stays stable** even if the list is later re-sorted — a BOM item number should not move. You do not set the number; it is frozen for you.

Refer to a component anywhere in the prose by its **short name**, its **MPN**, or a bare `[[dc…]]` handle — each raises the same hover card (description + MPN + manufacturer + datasheet link). Do **not** author a separate table of components: the components list is a *projection* rendered from these leaves (edit the data, not a derived table), so a table and the leaves can never drift.

Voice: precise and factual. Register and describe components here; the surrounding sections describe how the system is designed and built and reference the parts by name/number.

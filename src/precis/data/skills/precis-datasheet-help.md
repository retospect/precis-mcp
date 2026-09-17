---
id: precis-datasheet-help
title: precis — reading datasheets for PCB design
summary: read a component datasheet as searchable chunks to pull pinouts, supply ranges, decoupling guidance and application circuits, then turn that into net classes and measures. The electronics sibling of the paper kind; ingested by the same Marker→chunks pipeline.
answers:
  - how do I read a component datasheet as searchable chunks?
  - where does a datasheet ref come from — do I have to upload it myself?
  - what should I pull out of a datasheet and where does it go?
applies-to: get/search (kind='datasheet'); feeds pcb net-class + measures
status: active
tags: [design]
kinds: [datasheet, pcb, part]
---

# precis-datasheet-help — turn a datasheet into design decisions

A `datasheet` is a component document (electronics sibling of `paper`),
ingested the same way — embed / keywords / TOC / semantic search for free.
It's **evidence, read-only** (`supports_put=False`) — you mine it for the
facts a `pcb` design needs, you don't author it.

## Read one — `get` / `search`

```python
get(kind='datasheet')                            # list ingested
get(kind='datasheet', id='<slug>')               # one document
get(kind='datasheet', id='<slug>', view='toc')   # section/keyword TOC
search(kind='datasheet', q='I2C address timing', ...)
```

Paper addressing applies (`<slug>~N`, `/toc`, per-chunk search). Datasheets
are scoped out of `search(kind='paper')` and vice-versa.

## What to pull (and where it goes)

| From the datasheet | Into the design |
|--------------------|-----------------|
| **Pinout / pin functions** | `pins:[{name,tags}]` + net/class ([[precis-net-class-help]]) |
| **Absolute-max / supply range** | supply net `class:'power'` + `current` estimate |
| **Recommended decoupling** ("100 nF per VDD + 10 µF bulk") | bypass caps + `proximity` measure ([[precis-decoupling-help]]) |
| **Bus details** (I²C address, max SCK) | bus net class + pull-ups ([[precis-i2c-help]], [[precis-spi-help]]) |
| **Typical application circuit** | reference topology to capture |
| **Thermal / current** | trace `current`, a `thermal` measure (phase 2) |

## Where a datasheet comes from

Most arrive from a part's `datasheet_url` or an operator drop — ask an
operator if one's missing (`docs/runbooks/datasheet-ops.md`). Link it to
the part, and set vendor/subtype/part (flows into the exported citation):

```python
link(kind='datasheet', id='<slug>', rel='datasheet-of', to='part:<C-number>')
edit(kind='datasheet', id='<slug>', vendor='Espressif Systems',
     subtype='app-note', part_lcsc='C2934569')
```

## The move

Encode what you read as net **classes** ([[precis-net-class-help]]) and
**measures** ([[precis-measures-help]]) on the `pcb` design
([[precis-pcb-help]]) — the datasheet is the *why*, the netlist is the
*what*.

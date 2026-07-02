---
id: precis-datasheet-help
title: precis — reading datasheets for PCB design
summary: read a component datasheet as searchable chunks to pull pinouts, supply ranges, decoupling guidance and application circuits, then turn that into net classes and measures. The electronics sibling of the paper kind; ingested by the same Marker→chunks pipeline.
applies-to: get/search (kind='datasheet'); feeds pcb net-class + measures
status: active
---

# precis-datasheet-help — turn a datasheet into design decisions

A `datasheet` is a component document (the electronics sibling of `paper` /
`cfp`): ingested by the **same Marker → chunks pipeline**, so it gets embed /
keywords / TOC / semantic search for free, and reads in the same two-pane
reader. It is **evidence, read-only** (`supports_put=False`) — you don't author
it, you mine it for the facts a `pcb` design needs.

## Read one — `get` / `search`

```python
get(kind='datasheet')                       # list ingested datasheets
get(kind='datasheet', id='<slug>')          # the document
get(kind='datasheet', id='<slug>', view='toc')        # section/keyword TOC
search(kind='datasheet', q='I2C address timing', ...)  # over datasheet chunks
```

It's a paper-family reader, so the `paper` addressing works (`<slug>~N` for a
chunk range, `/toc`, per-chunk search). Datasheets are scoped **out** of
academic `search(kind='paper')` and vice-versa — a datasheet is never cited as
research evidence.

## What to pull (and where it goes)

| From the datasheet | Into the design |
|--------------------|-----------------|
| **Pinout / pin functions** | component `pins:[{name,tags}]` + which `net`/`class` each joins ([[precis-net-class-help]]) |
| **Absolute-max / supply range** | the supply net `class:'power'` + `current` estimate |
| **Recommended decoupling** ("100 nF per VDD + 10 µF bulk") | the bypass caps + a `proximity` measure ([[precis-decoupling-help]]) |
| **Bus details** (I²C address, max SCK) | the bus net class + pull-up values ([[precis-i2c-help]], [[precis-spi-help]]) |
| **Typical application circuit** | the reference topology to capture |
| **Thermal / current** | trace `current`, a `thermal` measure (phase 2) |

## Where a datasheet comes from

Every catalog `part` row carries a `datasheet_url` ([[precis-part-select-help]]).
Lazy ingestion from that URL — fetch on first reference, link `datasheet-of` /
`has-datasheet` to the `part` — is the planned path; until it's wired, ingest a
datasheet PDF through the normal `precis add` route and read it here.

## The move

Read the datasheet → decide pin functions, supply, decoupling, bus rules →
encode them as net **classes** ([[precis-net-class-help]]) and **measures**
([[precis-measures-help]]) on the `pcb` design ([[precis-pcb-help]]). The
datasheet is the *why*; the netlist + measures are the *what*. When
`view='trace'` hits a multi-pin part it stops and asks you for the next hop —
that hop is in the datasheet.

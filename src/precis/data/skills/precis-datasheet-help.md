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

**Drop a PDF into `<inbox>/datasheets/`** — the `precis watch` inbox routes
that subtree through the paper Marker→chunks pipeline stamped as `datasheet`
(exactly like `<inbox>/cfp/` for a `cfp`). On the cluster the inbox is
`$PRECIS_WATCH_INBOX` (`/opt/nas/botshome/papers/inbox`), so the drop dir is
`/opt/nas/botshome/papers/inbox/datasheets/`; locally it's the `datasheets/`
subdir of whatever you pass to `precis watch <dir>`. The usual `tagging/`
sentinel works too (`datasheets/tagging/<topic>/foo.pdf` → `topic:<topic>`).

Every catalog `part` row also carries a `datasheet_url`
([[precis-part-select-help]]); link a datasheet to the part it documents with
`link(kind='datasheet', id='<slug>', rel='datasheet-of', to='part:<C-number>')`
(inverse `has-datasheet`, seeded by migration 0054). Lazy fetch-on-first-
reference from `datasheet_url` is still the planned automation.

## Read it in the browser

Datasheets have a dedicated two-pane reader at **`/datasheets/<slug>`**
(vendored pdf.js on the right, Navigate/Jump/in-doc search on the left — the
same reader as `/papers` and `/pres`). It's reached from Drive, cross-kind
search (`/items`), or the part it documents — there is no standalone
`/datasheets` list page (like `/pres`).

The **Meta** tab is datasheet-shaped (not the paper's bibliographic form): it
edits three `meta` fields —

- **vendor** (manufacturer, e.g. `Espressif Systems`),
- **sub-type** — one of `datasheet` / `app-note` / `errata` / `reference-manual`
  (the one-kind-for-the-family selector), and
- **part** — the LCSC C-number of the documented part.

Set them here or via `edit(kind='datasheet', id='<slug>', vendor=…,
subtype='app-note', part_lcsc='C2934569')`. They **flow into citations**: a
cited datasheet renders as a BibTeX `@manual` with `organization={vendor}` +
`howpublished={<sub-type label>}` (LaTeX/PDF export) and as a
"Vendor (year). Title. [Sub-type] Part C…" line in the docx **References**
(`export.latex.build_bib` / `export.docx`). (The part is stored on `meta`, not
yet a `datasheet-of` graph edge — `part` is a catalog kind, not a `refs` row.)

## The move

Read the datasheet → decide pin functions, supply, decoupling, bus rules →
encode them as net **classes** ([[precis-net-class-help]]) and **measures**
([[precis-measures-help]]) on the `pcb` design ([[precis-pcb-help]]). The
datasheet is the *why*; the netlist + measures are the *what*. When
`view='trace'` hits a multi-pin part it stops and asks you for the next hop —
that hop is in the datasheet.

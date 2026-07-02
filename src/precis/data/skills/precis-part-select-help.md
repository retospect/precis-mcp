---
id: precis-part-select-help
title: precis — selecting JLCPCB parts for a PCB
summary: pick real, manufacturable components for a pcb design from the LCSC/JLCPCB catalog — search by parametrics, prefer Basic + high-turnover (not the last reel), read one part by C-number, and let the footprint auto-stamp onto your component. Covers LCSC, JLCPCB assembly, Basic vs Extended, stock, BOM cost.
applies-to: get/search (kind='part'); feeds put (kind='pcb')
status: active
---

# precis-part-select-help — pick parts that JLCPCB can actually build

A `part` is reference data in the LCSC/JLCPCB catalog, addressed by its **LCSC
C-number** (`C25804`). It is **ingest-only** (loaded from the `jlcparts` dump
by the `parts_refresh` worker) — you `get` and `search` it, never `put` it.
The whole catalog is **JLCPCB-assemblable by definition**, so a part you find
here can be placed and soldered by the fab. This skill feeds the `part` field
of a `pcb` component ([[precis-pcb-help]]).

## Search the catalog — `search(kind='part', q=…)`

```python
search(kind='part', q='0.1uF 0402 X7R 16V')      # a bypass cap
search(kind='part', q='ESP32-C3 module')
search(kind='part', q='10k 0402 resistor')
search(kind='part', q='3.3V LDO 500mA SOT-23')
```

The selector **hard-filters to assemblable parts** and ranks them:

1. **Basic first.** JLCPCB stocks *Basic* parts on every assembly line for
   free; *Extended* parts cost a per-reel loading fee and add risk. Prefer
   Basic unless the spec needs the Extended part.
2. **Then turnover, not raw stock.** Ranking uses a derived *restock* signal
   (how often the part's stock rises across daily dumps) + a smoothed stock
   level — **not** the instantaneous count. This steers you away from the
   "last reel" (high stock today, never restocked, gone next week) toward parts
   that keep coming back.
3. **Then cheaper.** The row shows the cheapest unit price across qty breaks.

Columns: `lcsc · mfr_part · description · basic · stock · restocks · package ·
$ea`. Pick the top Basic row that matches your parametrics + footprint.

> If a search returns nothing, the catalog may simply be empty on this host —
> it's populated by `precis pcb refresh-parts` (the jlcparts dump). Say so
> rather than inventing a C-number.

## Read one part — `get(kind='part', id='C…')`

```python
get(kind='part', id='C25804')   # mfr part, assemblable, basic, stock, package, height, datasheet, restocks
```

Use this to confirm a candidate before committing it to a design — especially
`package` (must match your footprint) and `basic`.

## Use it in a design — the auto-stamp

When you give a `pcb` component a `part` C-number, precis **auto-stamps** the
footprint / height / courtyard from the catalog onto the component, so the
design stays self-contained even if the catalog later churns:

```python
put(kind='pcb', id='s', args={'components':[
  {'refdes':'C1', 'label':'100nF 0402', 'part':'C1525',
   'pins':[{'name':'1'},{'name':'2'}]},   # footprint '0402' + height copied from C1525
]})
```

You can still pass an explicit `footprint`/`height_mm`/`courtyard` to override.
Real pad geometry (the pin-name→pad map used by the DSN exporter) is fetched
lazily from `easyeda2kicad` and cached; until that runs the exporter falls back
to placeholder pads (clearly labelled).

## Policy in one line

**Basic + high-turnover + footprint-matches + cheapest** — in that priority.
The JLCPCB order is the final availability gate; turnover ranking just makes it
unlikely you picked a part that's about to vanish.

## Selecting per function (typical board)

| Function | Search | Prefer |
|----------|--------|--------|
| Bypass / decoupling cap | `100nF 0402 X7R` | Basic, 0402, X7R |
| Bulk cap | `10uF 0805 X5R 6.3V` | Basic |
| Pull-up / series resistor | `4.7k 0402` / `10k 0402` | Basic, 1% |
| LDO regulator | `3.3V LDO 500mA SOT-23` | check dropout + Iq |
| MCU / module | by part name | module = fewer support parts |
| LED + resistor | `0603 green LED` | Basic |

See [[precis-decoupling-help]] for *how many* caps and where.

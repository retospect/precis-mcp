---
id: precis-part-select-help
title: precis — selecting JLCPCB parts for a PCB
summary: pick real, manufacturable components for a pcb design from the LCSC/JLCPCB catalog — fit first, then Basic + high-turnover (not the last reel), consolidating on one family and one package size (0402 default); read one part by C-number and let the footprint auto-stamp onto your component. Covers LCSC, JLCPCB assembly, Basic vs Extended, stock, popularity, BOM consolidation, cost.
answers:
  - how do I search the LCSC/JLCPCB catalog for a real, buyable part?
  - how do I use a selected part in a PCB design?
  - how do I pick parts for a typical board function, like decoupling or an MCU?
applies-to: get/search (kind='part'); feeds put (kind='pcb')
status: active
---

# precis-part-select-help — pick parts that JLCPCB can actually build

A `part` is reference data in the LCSC/JLCPCB catalog, addressed by its **LCSC
C-number** (`C25804`). It is **ingest-only** (loaded via `precis pcb
refresh-parts` / the `parts_refresh` worker, from the JLCPCB Open API or the
community `jlcparts` dump) — you `get` and `search` it, never `put` it.
The whole catalog is **JLCPCB-assemblable by definition**, so a part you find
here can be placed and soldered by the fab. This skill feeds the `part` field
of a `pcb` component ([[precis-pcb-help]]).

## Search the catalog — `search(kind='part', q=…)`

```python
search(kind="part", q="0.1uF 0402 X7R 16V")  # a bypass cap
search(kind="part", q="ESP32-C3 module")
search(kind="part", q="10k 0402 resistor")
search(kind="part", q="3.3V LDO 500mA SOT-23")
```

The selector **hard-filters to assemblable parts** and ranks them:

1. **Fit is a gate, not a tiebreak.** A candidate must first satisfy the
   *electrical* spec — value, tolerance, voltage and power rating,
   dielectric (X7R/X5R for anything that matters; never Y5V), temperature
   range — and the package. A cheaper, more popular part that misses a
   rating is not a candidate at all. Rank only what fits.
2. **Basic first.** JLCPCB stocks *Basic* parts on every assembly line for
   free; *Extended* parts cost a per-reel loading fee and add risk. Prefer
   Basic unless the spec needs the Extended part.
3. **Then turnover, not raw stock.** High stock volume is a good proxy for
   "everyone uses this part" — and popular is safer: it stays orderable, it
   is what JLC's line already runs, and its quirks are known. But raw volume
   alone has a failure mode, so ranking uses a derived *restock* signal (how
   often stock rises across daily dumps) + a smoothed level, **not** the
   instantaneous count. That steers you away from the "last reel" (high
   stock today, never restocked, gone next week) toward parts that keep
   coming back.
4. **Then cheaper.** The row shows the cheapest unit price across qty breaks.

Columns: `lcsc · mfr_part · description · basic · stock · restocks · package ·
$ea`. Pick the top Basic row that matches your parametrics + footprint.

> If a search returns nothing, the catalog may simply be empty on this host —
> it's populated by `precis pcb refresh-parts` (the JLCPCB Open API, falling
> back to the community jlcparts dump without API credentials). Say so
> rather than inventing a C-number.

## Read one part — `get(kind='part', id='C…')`

```python
get(
    kind="part", id="C25804"
)  # mfr part, assemblable, basic, stock, package, height, datasheet, restocks
```

Use this to confirm a candidate before committing it to a design — especially
`package` (must match your footprint) and `basic`.

## Use it in a design — the auto-stamp

When you give a `pcb` component a `part` C-number, precis **auto-stamps** the
footprint / height / courtyard from the catalog onto the component, so the
design stays self-contained even if the catalog later churns:

```python
put(
    kind="pcb",
    id="s",
    args={
        "components": [
            {
                "refdes": "C1",
                "label": "100nF 0402",
                "part": "C1525",
                "pins": [{"name": "1"}, {"name": "2"}],
            },  # footprint '0402' + height copied from C1525
        ]
    },
)
```

You can still pass an explicit `footprint`/`height_mm`/`courtyard` to override.
Real pad geometry (the pin-name→pad map used by the DSN exporter) is fetched
lazily from `easyeda2kicad` and cached; until that runs the exporter falls back
to placeholder pads (clearly labelled).

## Two rules the ranking can't apply for you

The ranking sees one search at a time. These two need *your* view of the whole
board — apply them yourself when choosing among the top rows.

**Default to 0402, and stay there.** For passives prefer SMT, smallest that
still fits the job: **0402 is the default**; go 0603/0805 only for a reason you
can name (power dissipation, voltage derating, hand-rework, a bulk cap's
capacitance). Don't mix 0402 and 0603 for the same function across a board —
one size means one feeder, one reel, one placement setup.

**Stay in one family.** Prefer a part from a series you've *already picked* on
this board over an equally-good stranger: same resistor series (one
manufacturer's 1% 0402 line for every resistor), same cap dielectric line, same
logic family, same connector series. Consolidation pays three ways — fewer
distinct SKUs means fewer Extended-part loading fees, one set of known
characteristics (tolerance, tempco, ESR) instead of several, and a reorder that
doesn't re-litigate part choices. When a family member is missing a value you
need, take the family's next value up before you leave the family.

## When the part you want isn't well stocked

Thin stock is a design signal, not just a purchasing problem. Work down this
ladder — the top rungs cost nothing, the bottom ones cost board area.

1. **Same-footprint alternate.** Most substitutes share footprint *and*
   pinout (a dozen vendors' 100nF 0402 X7R; SOT-23-5 LDOs with identical
   pinouts). Nothing on the board changes — record the approved alternates on
   the BOM line and move on. Every alternate must match the primary's
   footprint and pin-map; if it doesn't, it belongs on a lower rung.
2. **Take the bigger part.** A commodity part with features you don't need
   usually beats an exact-fit part nobody stocks: an 8-bit shift register
   where you need 5 bits, a fatter MCU, a higher-voltage or
   higher-current-rated device. Unused capability is free; unavailability is
   not. Check only that the extras are genuinely inert (no mandatory
   support parts, no quiescent-current surprise).
3. **Split one exotic part into two commodity ones.** Two cascaded 74HC595s
   instead of a 16-bit driver; an MCU pin plus a discrete FET instead of a
   specialised driver IC. Costs a little area and routing, buys parts that
   are *always* in stock.
4. **Two footprints, side by side, one populated (DNP).** Place both land
   patterns **normally, not overlapping** — overlapping pads bring bridging
   risk and awkward paste apertures for no gain. Populate variant A or B and
   note which; the other stays unpopulated and out of the BOM/CPL. Costs
   board area and routing to both sites, so reserve it for a genuinely
   single-sourced critical part where the alternate is a different package.
5. **Accept the thin-stock part** — deliberately, with the risk stated in
   the component's `note`, not by default.

**"Well stocked" is a ratio, not a number.** Compare stock against
*this build*: qty × per-board count, with a healthy multiple. 5 000 units is
enormous for ten prototypes and thin for a 10 k run. And remember rule 2
above: **Basic parts sit on JLC's line permanently**, so for most passives
"prefer Basic" already solves availability without any of this ladder.

## Policy in one line

**Fits the spec + Basic + same family + high-turnover + cheapest** — in that
priority. Fit is a gate; family keeps the BOM consolidated; turnover makes it
unlikely you picked a part about to vanish; the JLCPCB order is the final
availability gate.

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

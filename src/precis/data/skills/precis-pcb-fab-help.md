---
id: precis-pcb-fab-help
family: pcb
title: precis — export and order a pcb design (fab package, JLCPCB, enclosure)
summary: take a routed pcb design to fabrication — the export views (bom/cpl/netlist/dsn/mechanical/route/gerber), view='fab' writing the JLCPCB order package, op='order' dry-running or placing the JLCPCB order, view='availability' sweeping BOM stock/lifecycle, and the features array + CAD-enclosure bridge. Covers gerber export, pick-and-place CPL, Specctra DSN, Freerouting, JLCPCB ordering, EOL checks, enclosure hand-off.
answers:
  - how do I export gerbers / a BOM / a CPL / a DSN from a pcb design?
  - how do I order a board at JLCPCB (view='fab', op='order')?
  - how do I check BOM stock and lifecycle before ordering (view='availability')?
  - how do I hand board outline + heights to a cad enclosure?
applies-to: get/put (kind='pcb'); see also kind='cad', kind='part'
status: active
tags: [design]
kinds: [pcb]
---

# precis-pcb-fab-help — get the board fabricated

Downstream of authoring ([[precis-pcb-help]]) and place/route
([[precis-pcb-route-help]]): export is the only place the design leaves
the graph. Artifacts land under `<PRECIS_CORPUS_DIR>/pcb/<slug>/`
(override with `args={'dir':'…'}`).

## Export the design — `get(view=…)`

```python
get(kind="pcb", id="s", view="bom")  # JLCPCB BOM CSV (grouped designators)
get(
    kind="pcb", id="s", view="cpl"
)  # JLCPCB pick-and-place CSV (rotation converted to CCW)
get(kind="pcb", id="s", view="netlist")  # KiCad s-expr netlist
get(kind="pcb", id="s", view="dsn")  # Specctra .dsn (the autorouter's input)
get(
    kind="pcb", id="s", view="mechanical"
)  # outline + mounting holes + height-blocks → a cad enclosure
get(
    kind="pcb", id="s", view="route", args={"max_passes": 3}
)  # Freerouting place↔route round-trip
get(kind="pcb", id="s", view="gerber")  # the fab bundle: gerbers + drill, zipped
```

`view='gerber'` runs DRC first and **leads its response with the verdict**.
A board with DRC errors still exports, so you can look at it, but the
response opens with a `DRC FAILED` block naming the error count, the rules
and the first findings. Do not send that bundle to a fab: fix the board and
re-export until the first line reads `DRC: 0 errors`.

`view='route'` runs the place↔route hand-off: place → `.dsn` →
Freerouting → on an incomplete route, re-place (more iters) and re-route,
bounded. With no router installed it **degrades to a `.dsn`-only pass**
(open it in EasyEDA/KiCad as a manual escape hatch). `bom`/`cpl` warn
about unplaced or non-assemblable (no-LCSC) parts. An instance that is
etched copper rather than a part (a footprint whose pads are all `role:
'electrode'` or `'probe'`, such as an EWOD electrode array) is left out of
both: there is nothing to place or buy.

`view='bom'` also ends its **response** with a `## Consolidation` nudge
(the written CSV is untouched): same-package parts of one jlcparts class
differing only in a free parametric (`parts.params` — Resistance,
Capacitance) get a "one value?" line proposing the house-set or plurality
member with a one-sentence LLM verdict and its cited inputs; a
`kept-distinct:<label>:<label>[:<reason>]` tag on the design suppresses a
pair and prints the reason (pcb component types aren't refs, so the
link-door `rel='kept-distinct'` is the component kind's form). See
[[precis-component-help]] for the attribute-role model.

## Check the BOM is still buyable — `view='availability'` (the EOL sweep)

```python
get(kind="pcb", id="s", view="availability")  # stock/lifecycle per BOM line
```

One row per component type: `refdes · qty · lcsc · mfr_part · lib · stock ·
$ea · lifecycle · fetched`. `lib` is `basic`/`ext` (the JLCPCB library
class; a Basic part sits on the line free, an Extended part costs a
loading fee).
`lifecycle` is every signal the catalog row carries — `merged→<C-no>` (the
part was folded into a successor — strongest EOL tell), `alts`, `no-buy`,
`idle`, `est`, `dropped-from-dump`, `stock 0` — or `ok`. A line with no
LCSC number lists as such; a C-number absent from the catalog says `not in
catalog` (or `absent from jlcpcb.com` when the live check just ran).

Rows whose catalog data is older than ~6h — or missing — get one **bounded**
live refresh from the jlcpcb.com listing on the way out (≤12 C-numbers per
call; the head line reports `re-catalogued / absent / failed / skipped`).
The live listing carries no explicit "EOL" flag, so lifecycle is inferred
from the signals above + stock. Component `part:`/`part_lcsc:` values take
a bare `C<n>` in any case or a pasted lcsc.com/jlcpcb.com product URL
(extracted, never fetched). See [[precis-part-select-help]] for the
part-kind side.

## Order at JLCPCB — `view='fab'`, `op='order'`

`view='fab'` (args `{dir}`) writes `{slug}-fab.zip`, `{slug}-bom.csv`,
`{slug}-cpl.csv` — the zip is the order upload; BOM/CPL attach by hand at
jlcpcb.com checkout (the order API has no PCBA endpoints).

`put(args={'op':'order'})` builds the JLCPCB order payload — layers/size
from the board; `qty`, `thickness_mm`, `pcb_param={...}` (JLC's int craft
codes — surface finish, color), `smt_stencil`, `shipping_address`,
`shipping_method`, `country`/`postcode`/`city`. `dry_run` defaults true:
prints the exact `pcb/create` body and attempts the read-only quote.
Placing it is explicit: `{'op':'order','dry_run':False,
'confirm':'add-to-cart','shipping_address':{...}}` uploads the zip, quotes
with the file key, then `pcb/create` — an **unpaid order awaiting your
checkout** (the API's cart equivalent; there is no pay or submit endpoint
and this op exposes none). Batch/order id lands in the board's meta as
`jlc_order`; a repeat needs `reorder=True`.

## Drive an enclosure — `features` + `view='mechanical'`

Add non-electrical geometry so the board can drive an enclosure:

```python
put(
    kind="pcb",
    id="s",
    args={
        "features": [
            {
                "ftype": "outline",
                # corner_radius_mm (optional) rounds every outline corner
                # (fillet, polygonized); pours/DRC/silk all inherit it.
                "geom": {"path": [[0, 0], [30, 0], [30, 20], [0, 20]],
                         "corner_radius_mm": 2.0},
            },
            # bare screw hole (unplated; copper must clear it — DRC npth rule)
            {"ftype": "mounting_hole", "x": 2, "y": 2, "geom": {"diameter": 3.2}},
            # solder-on nut: plated hole + copper ring on every layer
            # (rendered, gerber'd, and cleared like a pad; router+pours
            # avoid both kinds automatically)
            {"ftype": "mounting_hole", "x": 28, "y": 18,
             "geom": {"diameter": 5.6, "ring_dia_mm": 8.0, "plated": True,
                      "style": "solder_nut_m4"}},
        ]
    },
)
```

`view='mechanical'` emits a JSON profile (outline + holes + component
height-blocks) a `cad` enclosure references (see [[precis-cad-help]]).

## See also

- [[precis-pcb-help]] — author the netlist + placement this exports.
- [[precis-pcb-route-help]] — the enqueued place/route ops a fab-ready
  board has already passed.
- [[precis-pcb-check-help]] — the check surface behind `view='gerber'`'s
  DRC verdict.
- [[precis-cad-help]] — the enclosure side of `view='mechanical'`.
- [[precis-part-select-help]] — pick assemblable parts the availability
  sweep will still find buyable.

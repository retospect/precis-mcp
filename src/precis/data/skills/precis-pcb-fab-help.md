---
id: precis-pcb-fab-help
family: pcb
title: precis — export and order a pcb design (fab package, JLCPCB, enclosure)
summary: take a routed pcb design to fabrication — the export views (bom/cpl/netlist/dsn/mechanical/gerber/epro), view='epro' writing an EasyEDA Pro .epro2 and verifying it reads back, view='fab' writing the JLCPCB order package, op='order' dry-running or placing the JLCPCB order, view='availability' sweeping BOM stock/lifecycle, and the features array + CAD-enclosure bridge. Covers gerber export, pick-and-place CPL, Specctra DSN, JLCPCB ordering, EOL checks, enclosure hand-off.
answers:
  - how do I export gerbers / a BOM / a CPL / a DSN from a pcb design?
  - how do I export a board to EasyEDA Pro and check the file is right?
  - how do I download a board's .epro2 export, or copy a board?
  - how do I order a board at JLCPCB (view='fab', op='order')?
  - how do I check BOM stock and lifecycle before ordering (view='availability')?
  - how do I hand board outline + heights to a cad enclosure?
  - where does a part's refdes label go, and why is a silk outline open at the board edge?
applies-to: get/put (kind='pcb'); see also kind='cad', kind='part'
status: active
tags: [design]
kinds: [pcb]
---

# precis-pcb-fab-help — get the board fabricated

Downstream of authoring and placement
([[precis-pcb-help]]): export is the only place the design leaves
the graph. Artifacts land under `<PRECIS_CORPUS_DIR>/pcb/<slug>/`, or
the server's temp dir when that is unset (override with
`args={'dir':'…'}` — must resolve under that export root or the system
temp dir; anything else refuses). That path is on the serving host: the
MCP returns the
path, not the file. BOM and CPL responses inline a preview; gerber and
fab do not. epro names a web UI download path (below).

## Export the design — `get(view=…)`

```python
get(kind="pcb", id="s", view="bom")  # JLCPCB BOM CSV (grouped designators)
get(
    kind="pcb", id="s", view="cpl"
)  # JLCPCB pick-and-place CSV (rotation converted to CCW)
get(kind="pcb", id="s", view="netlist")  # KiCad s-expr netlist
get(kind="pcb", id="s", view="dsn")  # Specctra .dsn (an external autorouter's input)
get(
    kind="pcb", id="s", view="mechanical"
)  # outline + mounting holes + height-blocks → a cad enclosure
get(kind="pcb", id="s", view="gerber")  # the fab bundle: gerbers + drill, zipped
```

`view='gerber'` runs DRC first and **leads its response with the verdict**.
A board with DRC errors still exports, so you can look at it, but the
response opens with a `DRC FAILED` block naming the error count, the rules
and the first findings. Do not send that bundle to a fab: fix the board and
re-export until the first line reads `DRC: 0 errors`.

Synthesized (guessed) pad bounds refuse the export. For a look anyway pass
`args={'allow_synthesized': True}` — the response opens `PREVIEW` and the
file is named `{slug}-preview.zip`, never the order-upload `-fab.zip`, so a
preview can't be sent to a fab by mistake. `view='fab'` and `op='order'`
never accept the flag; the real fix is `op='footprint'` with the part's
LCSC number (see [[precis-pcb-check-help]]).

**Silkscreen is generated, not authored.** Each part gets one closed
outline drawn two silk line widths clear of its own pads. Close
neighbours bend their outlines toward each other but keep two line widths
apart, or share one wall. An edge-mated part whose outline would cross
the board edge gets one clean gap at the edge. A routed via on an
outline breaks it there (silk never prints on copper). The refdes label
goes **outside** the outline, two line widths clear of every outline and
never on a pad, via or other label.
Spots are tried in this fixed order, in board orientation (corners sit
just past that side of the outline, flush with its left or right edge):

1. bottom-left (the default)
2. bottom-right
3. top-left
4. top-right
5. left
6. right
7. top
8. bottom

Past those it tries spots further out, walking outward from bottom-left.
A label never goes inside the outline — ink under the assembled part is
invisible, so a part with no clear outside spot drops its label and DRC
reports `silk_missing`. To move one label by hand, set a per-part spot:
`put(kind='pcb', id='<slug>', args={'op':'move','refdes':'U1','label_spot':'top-right'})`
(any of the eight names above; `null` clears it).

`view='dsn'` writes the Specctra `.dsn` an external autorouter
(EasyEDA/KiCad/Freerouting) reads — the manual escape hatch.
`bom`/`cpl` warn
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

## Hand the board to EasyEDA Pro — `view='epro'`

```python
get(kind="pcb", id="s", view="epro")  # writes {slug}.epro2
get(kind="pcb", id="s", view="epro", args={"verify": True})  # + diff
```

The file carries the board only: outline, parts with their footprints,
pads, nets, tracks, vias, pour zones, silk strokes and editable
designators (Pro draws them in its own font) and the auto-placed board
fiducials, filed as `FID<n>` free pads on the outer layers. It has no
schematic, on purpose: Pro reports a clean import with no schematic pages;
do not run "Update PCB from schematic" there, it rewrites the netlist. Every dropped
item is a `⚠️` line in the response (an unplaced part, a pad with no
placed part, a blind via written through-hole). Synthesized pads refuse
the export unless `args={'allow_synthesized': True}`.

`args={'verify': True}` re-reads the written bytes through the
importer's own reader and diffs them against the board: parts (position,
rotation, side, LCSC), pads (position, net, shape, size, drill), net
declarations, track segments, vias, pour count, outline, and an imported
board's rule table. Positions pass within 0.5 um. It ends `verify: N ok,
0 FAIL`; any `FAIL` line names what the file does not carry.

The response's `sha256:` identifies the exact bytes, and its `download`
line is a web UI path (`/pcb/<slug>/export.epro2?sha=…`) for a human's
browser: it rebuilds the same bytes on request, and answers 409 once the
board has changed. Agents cannot fetch web UI paths (401); hand the path
to the human. The board page links the same download.

Copy a board — `op='fork'`, not a re-import:

```python
put(kind="pcb", id="s-copy", args={"op": "fork", "from": "s"})
```

`fork` is the lossless copy: every pcb table row (parts with
LCSC/footprint/poses, pins, all nets — single-pin ones included —,
routed copper, vias, zones, stackup, rules) in one transaction, with
`derived-from` provenance and the source untouched.
`args={'dry_run': True}` reports the per-table counts without writing.
`op='import_epro'` refuses a board source — the .epro2 round-trip drops
part/LCSC fields, routed copper and sub-2-pin nets. It is for EasyEDA
interop only: a real `.epro2` file imports with the CLI
(`precis pcb import-epro`, see [[precis-pcb-help]]).

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
`idle`, `est`, `stock-loss N` (units written off as warehouse loss on the
vendor listing — the `lossNumber` field), `dropped-from-dump`, `stock 0` —
or `ok`. A line with no
LCSC number lists as such; a C-number absent from the catalog says `not in
catalog` (or `absent from jlcpcb.com` when the live check just ran).

Rows whose catalog data is older than ~6h — or missing — get one **bounded**
live refresh from the JLCPCB Open API on the way out (≤12 C-numbers per
call; the head line reports `re-catalogued / absent / failed / skipped`).
The live listing carries no explicit "EOL" flag, so lifecycle is inferred
from the signals above + stock. Component `part:`/`part_lcsc:` values take
a bare `C<n>` in any case or a pasted lcsc.com/jlcpcb.com product URL
(extracted, never fetched). See [[precis-part-select-help]] for the
part-kind side.

## Order at JLCPCB — `view='fab'`, `op='order'`

`view='fab'` (args `{dir}` — under the export root only) writes `{slug}-fab.zip`, `{slug}-bom.csv`,
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
            # (rendered, gerber'd, and cleared like a pad; pours and DRC
            # clear both kinds automatically)
            {"ftype": "mounting_hole", "x": 28, "y": 18,
             "geom": {"diameter": 5.6, "ring_dia_mm": 8.0, "plated": True,
                      "style": "solder_nut_m4"}},
        ]
    },
)
```

### Corner mounting nuts

`put(kind='pcb', id='s', args={'op': 'corner_nuts'})` replaces the outline
with a rounded one and puts four SMT nuts (SMTSO family; default Sinhoo
SMTSO3030CTJ, LCSC C2915308, M3 x 3 through-thread) one per corner of a
plain rectangular outline. Each nut sits on the arc that rounds its corner:
an arc centred on the nut and tangent to both edges puts the nut at (R, R),
so `corner_radius_mm` equals the edge distance. Default R is the larger of
5.0 mm (PEM-style M3 nuts want 4.8-5.6 mm centre to edge) and body radius +
0.5 mm. Override with `radius_mm`, `hole_dia_mm`, `ring_dia_mm`,
`body_dia_mm` (datasheet values for another nut). The sizes default to the
SMTSO M3 table row (hole 4.22, solder pad 6.2, flange 5.56 mm); that table
was read from OCR text, so confirm it against the manufacturer PDF. Re-running replaces the
earlier corner nuts; other mounting holes stay. A board that does not ask
gets none.

- Pro: no sharp corner to chip or catch; the rounding removes only
  R^2 (1 - pi/4) per corner (~5.4 mm^2 at R = 5 mm), area that sits inside
  the nut keep-out anyway, so routing room is almost unchanged.
- Con: a rounded outline is routed, not V-scored, and panels need slightly
  more spacing. On a small board that is routed anyway this costs nothing.
- The nut is a plated hole with a copper land on every layer: pours and
  DRC clear it like a pad.

`view='mechanical'` emits a JSON profile (outline + holes + component
height-blocks) a `cad` enclosure references (see [[precis-cad-help]]).

## See also

- [[precis-pcb-help]] — author the netlist + placement this exports.
- [[precis-pcb-topo-help]] — the topo placer opt-in; routing is retired,
  so a fab export today carries pads, authored pours and stored copper.
- [[precis-pcb-check-help]] — the check surface behind `view='gerber'`'s
  DRC verdict.
- [[precis-cad-help]] — the enclosure side of `view='mechanical'`.
- [[precis-part-select-help]] — pick assemblable parts the availability
  sweep will still find buyable.

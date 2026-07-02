---
id: precis-pcb-help
title: precis — the PCB kind (electronics design you read as a graph)
summary: design a circuit board the LLM authors in batch and reads as a traversable netlist graph — components/pins/nets/placement, never pixels; pick JLCPCB-assemblable parts, place to minimise crossed wires, then export BOM/CPL/DSN and route with Freerouting. Covers schematic capture, netlist, footprints, ratsnest, autoplace, gerbers, EDA/CAD for circuits.
applies-to: get/search/put/delete (kind='pcb'); see also kind='part', kind='datasheet'
status: active
---

# precis-pcb-help — design circuits the LLM can *read*

A `pcb` design is a **netlist + placement graph** (ADR 0042): component
*types* that own pins, *instances* (refdes) placed in 2-D, *nets* that wire
pins together, and *measures* (design intent). You **author it in batch** and
**read it back as a graph** — "what's on U1's SCL pin? → which net? → who else
is on that net?" — and you **see geometry as numbers** (crossed airwires,
gaps, DRC), never a rendered board. Postgres is canonical; gerbers / BOM / the
autorouter are downstream *export*.

Units are **millimetres**. The board frame: origin at the **board-outline
corner**, **+X right, +Y up (north)**, rotation **clockwise from north**,
pivot = the component centroid. (Exporters convert to each fab's convention —
e.g. the JLCPCB CPL flips rotation to CCW for you.)

Four verbs, no new ones: `put` (create/extend), `get` (list / netlist TOC /
one instance / one net / an analysis / an export), `search` (by intent),
`delete` (soft-retire).

Related skills: [[precis-part-select-help]] (pick real parts),
[[precis-net-class-help]] (name + classify nets), [[precis-measures-help]]
(the "measuring tapes"), and the pattern playbooks [[precis-decoupling-help]],
[[precis-i2c-help]], [[precis-spi-help]], [[precis-datasheet-help]].

## Author a design — `put(id=<slug>, args={…})`

**Batch, re-runnable.** One `put` lays down components (with pins), nets, and
connections in one transaction; re-`put`ting the same slug **extends** it
(existing refdes/net names are reused, not duplicated).

```python
put(kind='pcb', id='sensor-node', args={
  'components': [
    {'refdes':'U1', 'label':'ESP32-C3', 'part':'C2838500', 'footprint':'QFN-32',
     'roles':['noisy'],
     'pins':[{'name':'VDD','tags':['power','3v3']}, {'name':'GND','tags':['gnd']},
             {'name':'SCL','tags':['i2c']}, {'name':'SDA','tags':['i2c']}]},
    {'refdes':'C1', 'label':'100nF 0402', 'part':'C1525', 'footprint':'0402',
     'pins':[{'name':'1'},{'name':'2'}], 'note':'VDD bypass for U1'},
    {'refdes':'R1', 'label':'4.7k 0402', 'part':'C25900', 'footprint':'0402',
     'pins':[{'name':'1'},{'name':'2'}]},
  ],
  'nets': [
    {'name':'VCC3V3', 'class':'power', 'current':0.5},
    {'name':'GND',    'class':'gnd'},
    {'name':'I2C_SCL','class':'i2c'},
  ],
  'connections': [
    {'net':'VCC3V3', 'refdes':'U1','pin':'VDD'},
    {'net':'VCC3V3', 'refdes':'C1','pin':'1', 'note':'bypass hi side'},
    {'net':'GND',    'refdes':'U1','pin':'GND'},
    {'net':'GND',    'refdes':'C1','pin':'2'},
    {'net':'I2C_SCL','refdes':'U1','pin':'SCL'},
    {'net':'I2C_SCL','refdes':'R1','pin':'1'},
  ],
})
```

Field notes:
- **component**: `refdes` (required), `label`, `part` (an LCSC C-number —
  footprint/height/courtyard are **auto-stamped** from the catalog, see
  [[precis-part-select-help]]), `footprint`, `pins` (`{name, pad?, tags?,
  description?, note?}`), placement `x`/`y`/`rot`/`layer` (`top`/`bottom`),
  `fixed` (`'xy'` or `'both'` — pins it against autoplace, for connectors /
  mounting / status LEDs), `roles` (free tags like `sensitive`/`noisy` that
  drive class-based measures), `note`.
- **net**: `name` is **required and meaningful** — the name *is* the intent
  (`I2C_SCL`, not `N$7`). `class` drives width / plane / measure defaults
  ([[precis-net-class-help]]); `current` (amps) sizes the trace; `width` (mm)
  overrides.
- **connection**: the `(net, refdes, pin)` triple. One physical pin is on **at
  most one net** (re-connecting moves it). A pin named in a connection but not
  declared on the component is **created on the fly**.
- A connection to an unknown **net** auto-creates the net; an unknown
  **refdes** is an error (declare the component first).
- Optional `measures` and `features` arrays — see below.

## Read it as a graph — `get`

```python
get(kind='pcb')                       # list designs
get(kind='pcb', id='sensor-node')     # netlist TOC: parts table + nets table (fanout, class, I, width)
get(kind='pcb', id='sensor-node#U1')  # ONE instance: each pin → its net → the neighbour instances
get(kind='pcb', id='sensor-node@I2C_SCL')  # ONE net: every (refdes, pin) on it
```

`#REFDES` is the **hop** — the core traversal move. `@NET` is the membership
view. Walk the design instance-by-instance instead of ingesting it whole.

## See the geometry — `get(view=…)` (the "eyes")

You never look at a render. You ask numeric questions:

```python
get(kind='pcb', id='s', view='crossings')   # crossed airwires — THE pre-routing objective (planes excluded)
get(kind='pcb', id='s', view='ratsnest')    # the MST airwires + total length (mm)
get(kind='pcb', id='s', view='feasibility') # coarse H/V Manhattan via-count estimate (NOT real routing)
get(kind='pcb', id='s', view='drc')         # DRC-lite findings (unplaced, off-board, overlaps…)
get(kind='pcb', id='s', view='proximity', args={'a':'U1','b':'C1'})   # centroid gap (mm)
get(kind='pcb', id='s', view='trace',     args={'net':'I2C_SCL'})     # logical hop through 2-pin series R/C
get(kind='pcb', id='s', view='measures')    # evaluate the design's measuring tapes
```

- **crossings** is the objective the placer minimises — fewer crossed wires =
  easier route. **Plane nets** (`gnd/ground/power/pwr/plane`) are excluded from
  the metric (they pour, they don't route point-to-point) but stay fully in the
  netlist.
- **trace** walks series 2-pin parts (a resistor/cap in line) automatically; a
  multi-pin part terminates the auto-walk — you supply the next hop from the
  datasheet ([[precis-datasheet-help]]).

## Place it — `put(args={'autoplace':{…}})`

```python
put(kind='pcb', id='s', args={'autoplace':{'iters':2000, 'seed':0}})
```

Simulated-annealing placement that minimises `crossings + ratsnest-length +
soft-measure penalty`. **`fixed` parts never move.** It reports
crossings/length/objective **before → after**. v1 optimises translation at
centroid granularity (rotation gains effect once real pad offsets are cached).
Iterate: autoplace → `view='crossings'` → pin a part with `fixed` → re-run.

## Export & route — `get(view=…)`

Export is the only place the design leaves the graph. Artifacts land under
`<PRECIS_CORPUS_DIR>/pcb/<slug>/` (override with `args={'dir':'…'}`).

```python
get(kind='pcb', id='s', view='bom')        # JLCPCB BOM CSV (grouped designators)
get(kind='pcb', id='s', view='cpl')        # JLCPCB pick-and-place CSV (rotation converted to CCW)
get(kind='pcb', id='s', view='netlist')    # KiCad s-expr netlist
get(kind='pcb', id='s', view='dsn')        # Specctra .dsn (the autorouter's input)
get(kind='pcb', id='s', view='mechanical') # outline + mounting holes + height-blocks → a cad enclosure (ADR 0041)
get(kind='pcb', id='s', view='route', args={'max_passes':3})  # Freerouting place↔route round-trip
```

`view='route'` runs the §9 hand-off: place → `.dsn` → Freerouting → on an
incomplete route, re-place (more iters) and re-route, bounded. With no router
installed it **degrades to a `.dsn`-only pass** (open it in EasyEDA/KiCad as a
manual escape hatch). `bom`/`cpl` warn about unplaced or non-assemblable
(no-LCSC) parts.

### Mechanical features — the CAD bridge

Add non-electrical geometry so the board can drive an enclosure:

```python
put(kind='pcb', id='s', args={'features':[
  {'ftype':'outline', 'geom':{'path':[[0,0],[30,0],[30,20],[0,20]]}},
  {'ftype':'mounting_hole', 'x':2, 'y':2, 'geom':{'diameter':3.2}},
]})
```

`view='mechanical'` emits a JSON profile (outline + holes + component
height-blocks) a `cad` enclosure references (see [[precis-cad-help]]).

## Find a design — `search`

```python
search(kind='pcb', q='I2C sensor node')              # by intent (hybrid)
search(kind='pcb', q='esp32 board', mode='semantic')
```

Each design carries one embeddable card (parts + net names), so search lands
on intent. `pcb` joins the cross-kind fan-out `search(kind='*', q='…')`.

## Delete

```python
delete(kind='pcb', id='sensor-node')   # soft-retire the whole design (recoverable)
```

## Canonical end-to-end (the v1 loop)

1. **Pick parts** — `search(kind='part', q='…')` for each function; prefer
   Basic + high-turnover ([[precis-part-select-help]]).
2. **Capture the netlist** — `put` components + nets + connections; name nets
   meaningfully + class them ([[precis-net-class-help]]).
3. **State intent** — add `measures` (keep the regulator off the antenna, the
   bypass cap *at* the pin) ([[precis-measures-help]]).
4. **Check connectivity** — `get(id=slug)`, `#REFDES` hops, `view='drc'`.
5. **Place** — `autoplace`, then `view='crossings'`; pin fixed parts; repeat.
6. **Export & route** — `view='route'`, then `view='bom'` + `view='cpl'` to
   order at JLCPCB; `view='mechanical'` for the enclosure.

## Scope (v1)

In: batch netlist authoring, graph traversal, the eyes (crossings / ratsnest /
DRC-lite / proximity / trace / measures / feasibility), simulated-annealing
autoplace, BOM/CPL/netlist/DSN/mechanical export, Freerouting round-trip,
JLCPCB-assemblable part selection with turnover ranking. **Deferred** (ADR 0042
Phase 2): the precis-owned shove router, real routed-length / length-matching
measures, rotation in the placer, full 3-D enclosure models, datasheet table
extraction.

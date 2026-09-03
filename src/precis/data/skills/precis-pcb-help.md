---
id: precis-pcb-help
title: precis — the PCB kind (electronics design you read as a graph)
summary: design a circuit board the LLM authors in batch and reads as a traversable netlist graph — components/pins/nets/placement, never pixels; pick JLCPCB-assemblable parts, place+route via enqueued worker jobs, then export BOM/CPL/DSN. Covers schematic capture, netlist, footprints, ratsnest, place/route, gerbers, EDA/CAD for circuits.
answers:
  - how do I author a PCB design from a netlist and placement graph?
  - how do I place and route a board (op='place'/op='route')?
  - how do I export a BOM/CPL/DSN or route the board?
  - how do I read a PCB design as a graph — pins, nets, neighbours?
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

Related skills: [[precis-pcb-route-help]] (place/route as enqueued jobs — the
`op=` surface, once the netlist exists), [[precis-part-select-help]] (pick
real parts), [[precis-net-class-help]] (name + classify nets),
[[precis-measures-help]] (the "measuring tapes"), and the pattern playbooks
[[precis-decoupling-help]], [[precis-i2c-help]], [[precis-spi-help]],
[[precis-datasheet-help]].

## Author a design — `put(id=<slug>, args={…})`

**Batch, re-runnable.** One `put` lays down components (with pins), nets, and
connections in one transaction; re-`put`ting the same slug **extends** it
(existing refdes/net names are reused, not duplicated).

```python
put(
    kind="pcb",
    id="sensor-node",
    args={
        "components": [
            {
                "refdes": "U1",
                "label": "ESP32-C3",
                "part": "C2838500",
                "footprint": "QFN-32",
                "roles": ["noisy"],
                "pins": [
                    {"name": "VDD", "tags": ["power", "3v3"]},
                    {"name": "GND", "tags": ["gnd"]},
                    {"name": "SCL", "tags": ["i2c"]},
                    {"name": "SDA", "tags": ["i2c"]},
                ],
            },
            {
                "refdes": "C1",
                "label": "100nF 0402",
                "part": "C1525",
                "footprint": "0402",
                "pins": [{"name": "1"}, {"name": "2"}],
                "note": "VDD bypass for U1",
            },
            {
                "refdes": "R1",
                "label": "4.7k 0402",
                "part": "C25900",
                "footprint": "0402",
                "pins": [{"name": "1"}, {"name": "2"}],
            },
        ],
        "nets": [
            {"name": "VCC3V3", "class": "power", "current": 0.5},
            {"name": "GND", "class": "gnd"},
            {"name": "I2C_SCL", "class": "i2c"},
        ],
        "connections": [
            {"net": "VCC3V3", "refdes": "U1", "pin": "VDD"},
            {"net": "VCC3V3", "refdes": "C1", "pin": "1", "note": "bypass hi side"},
            {"net": "GND", "refdes": "U1", "pin": "GND"},
            {"net": "GND", "refdes": "C1", "pin": "2"},
            {"net": "I2C_SCL", "refdes": "U1", "pin": "SCL"},
            {"net": "I2C_SCL", "refdes": "R1", "pin": "1"},
        ],
    },
)
```

Field notes:
- **component**: `refdes` (required), `label`, `part` (an LCSC C-number —
  footprint/height/courtyard are **auto-stamped** from the catalog, see
  [[precis-part-select-help]]), `footprint`, `pins` (`{name, pad?, tags?,
  description?, note?}`), placement `x`/`y`/`rot`/`layer` (`top`/`bottom`),
  `fixed` (`'xy'` or `'both'` — pins it against autoplace, for connectors /
  mounting / status LEDs), `roles` (free tags like `sensitive`/`noisy` that
  drive class-based measures), `note`. **Silk pin-1 marks**: a resistor,
  capacitor, inductor, or ferrite bead (refdes family R/C/L/FB) has no
  inherent polarity and gets no pin-1 indicator by default — set
  `polarized: true` for one that actually is (electrolytic/tantalum cap,
  polarized inductor) to keep the mark; a `label` containing ELEC/TANT/POL
  (case-insensitive) infers it too, so a well-named part needs no explicit
  flag. Every other family (D/Q/U/J/LED/…) is unaffected and always keeps
  its mark. **Placement constraints**: `group: "<name>"` +
  `group_offset: {x, y, rot}` lock components into one rigid body the
  autoplacer moves as a unit — the offsets are authored geometry (e.g. the
  two header rows of a daughterboard at their real row pitch); a `fixed`
  member pins the whole group. `pattern: "<name>"` + `pattern_instance: <n>`
  mark repeated subcircuits (channel 0..k of identical driver stages): every
  instance is laid out **identically** (instance 0's internal layout is
  stamped onto the rest and each tile then moves rigidly), so repeats read
  as clean tiles instead of four ad-hoc arrangements.
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
- Optional `net_classes` — `{name: rules}` per-design router/DRC rules
  (upsert; existing names not in the batch are left alone). `rules` is a
  free-form dict (`clearance_mm`, track width, via drill/annular, permitted
  layers…); a net's `net_class` joins this by name, a missing row means
  built-in defaults.
- Every design gets a default **board** (`pcb_boards`, name `'main'`, the
  4-layer rigid FR-4 stackup `F.Cu`/`In1.Cu(GND)`/`In2.Cu`/`B.Cu`) on first
  `put` — the netlist≠board hedge for future multi-board work; v1 is one
  board per design.
- **`nets[].domain`** — only `'electrical'` (the default) is accepted
  today; any other value is rejected.

## Read it as a graph — `get`

```python
get(kind="pcb")  # list designs
get(
    kind="pcb", id="sensor-node"
)  # netlist TOC: board/stackup + parts + nets (fanout, class, I, width) +
   # net_classes + route-status summary
get(
    kind="pcb", id="sensor-node#U1"
)  # ONE instance: each pin → its net → the neighbour instances
get(kind="pcb", id="sensor-node@I2C_SCL")  # ONE net: every (refdes, pin) on it
```

`#REFDES` is the **hop** — the core traversal move. `@NET` is the membership
view. Walk the design instance-by-instance instead of ingesting it whole.

## See the geometry — `get(view=…)` (the "eyes")

You never look at a render. You ask numeric questions:

```python
get(
    kind="pcb", id="s", view="crossings"
)  # crossed airwires — THE pre-routing objective (planes excluded)
get(kind="pcb", id="s", view="ratsnest")  # the MST airwires + total length (mm)
get(
    kind="pcb", id="s", view="feasibility"
)  # coarse H/V Manhattan via-count estimate (NOT real routing)
get(
    kind="pcb", id="s", view="drc"
)  # DRC-lite findings (unplaced, off-board, overlaps…)
get(
    kind="pcb", id="s", view="route-status"
)  # per-net route status: unrouted|sketched|realized|failed
get(
    kind="pcb", id="s", view="congestion"
)  # the last op='route' run's over-capacity-gap warnings (see precis-pcb-route-help)
get(
    kind="pcb", id="s", view="planes"
)  # authored plane assignments (op='plane_net') — which nets are plane-served
get(
    kind="pcb", id="s", view="proximity", args={"a": "U1", "b": "C1"}
)  # centroid gap (mm)
get(
    kind="pcb", id="s", view="trace", args={"net": "I2C_SCL"}
)  # logical hop through 2-pin series R/C
get(kind="pcb", id="s", view="measures")  # evaluate the design's measuring tapes
```

- **crossings** is the objective the placer minimises — fewer crossed wires =
  easier route. **Plane nets** (`gnd/ground/power/pwr/plane`) are excluded from
  the metric (they pour, they don't route point-to-point) but stay fully in the
  netlist.
- **trace** walks series 2-pin parts (a resistor/cap in line) automatically; a
  multi-pin part terminates the auto-walk — you supply the next hop from the
  datasheet ([[precis-datasheet-help]]).

## Place and route it — `put(args={'op':'place'|'route', …})`

Placement and routing run as **enqueued worker jobs** — never inline in this
call (a real board is minutes of compute, not milliseconds). `put` returns a
job id immediately; see **[[precis-pcb-route-help]]** for the full `op=` surface
(`place`/`route`, plus the inline edits `move`/`rip`/`pin_side`/`plane_net`/
`class_rules`), the congestion/planes read views, and what's still inert
(including its inert move classes `SIDE_FLIP`/`PIN_SWAP`).

```python
put(kind="pcb", id="s", args={"op": "place", "iters": 2000, "seed": 0})
# ... poll get(kind='job', id='<id>') or re-check view='crossings' ...
put(kind="pcb", id="s", args={"op": "route"})
```

`args={'autoplace': {...}}` is a **deprecated alias** for `op='place'` (same
enqueue, same params) — kept for one release, then removed.

## Export & route — `get(view=…)`

Export is the only place the design leaves the graph. Artifacts land under
`<PRECIS_CORPUS_DIR>/pcb/<slug>/` (override with `args={'dir':'…'}`).

```python
get(kind="pcb", id="s", view="bom")  # JLCPCB BOM CSV (grouped designators)
get(
    kind="pcb", id="s", view="cpl"
)  # JLCPCB pick-and-place CSV (rotation converted to CCW)
get(kind="pcb", id="s", view="netlist")  # KiCad s-expr netlist
get(kind="pcb", id="s", view="dsn")  # Specctra .dsn (the autorouter's input)
get(
    kind="pcb", id="s", view="mechanical"
)  # outline + mounting holes + height-blocks → a cad enclosure (ADR 0041)
get(
    kind="pcb", id="s", view="route", args={"max_passes": 3}
)  # Freerouting place↔route round-trip
```

`view='route'` runs the §9 hand-off: place → `.dsn` → Freerouting → on an
incomplete route, re-place (more iters) and re-route, bounded. With no router
installed it **degrades to a `.dsn`-only pass** (open it in EasyEDA/KiCad as a
manual escape hatch). `bom`/`cpl` warn about unplaced or non-assemblable
(no-LCSC) parts.

### Mechanical features — the CAD bridge

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

## Find a design — `search`

```python
search(kind="pcb", q="I2C sensor node")  # by intent (hybrid)
search(kind="pcb", q="esp32 board", mode="semantic")
```

Each design carries one embeddable card (parts + net names), so search lands
on intent. `pcb` joins the cross-kind fan-out `search(kind='*', q='…')`.

## Retire a design

```python
delete(kind="pcb", id="sensor-node")  # soft-retire the whole design (recoverable)
```

## Canonical end-to-end

1. **Pick parts** — `search(kind='part', q='…')` for each function; prefer
   Basic + high-turnover ([[precis-part-select-help]]).
2. **Capture the netlist** — `put` components + nets + connections; name nets
   meaningfully + class them ([[precis-net-class-help]]).
3. **State intent** — add `measures` (keep the regulator off the antenna, the
   bypass cap *at* the pin) ([[precis-measures-help]]).
4. **Check connectivity** — `get(id=slug)`, `#REFDES` hops, `view='drc'`.
5. **Place** — `op='place'` (enqueued), then `view='crossings'`; pin fixed
   parts; repeat. See [[precis-pcb-route-help]].
6. **Route** — `op='route'` (enqueued); check `view='route-status'` and
   `view='congestion'`; rip + re-pin + re-route on a failure
   ([[precis-pcb-route-help]]'s rip-up loop).
7. **Export & order** — `view='bom'` + `view='cpl'` to order at JLCPCB;
   `view='mechanical'` for the enclosure; `view='route'` (Freerouting) stays
   available as a demoted escape hatch.

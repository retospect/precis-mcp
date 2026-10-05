---
id: precis-pcb-help
title: precis — the PCB kind (electronics design you read as a graph)
summary: design a circuit board the LLM authors in batch and reads as a traversable netlist graph — components/pins/nets/placement, never pixels; pick JLCPCB-assemblable parts, place+route via enqueued worker jobs, then export BOM/CPL/DSN. Covers schematic capture, netlist, footprints, ratsnest, place/route, gerbers, EDA/CAD for circuits.
answers:
  - how do I author a PCB design from a netlist and placement graph?
  - how do I place and route a board (op='place'/op='route')?
  - how do I export a BOM/CPL/DSN or route the board?
  - how do I read a PCB design as a graph — pins, nets, neighbours?
  - view='drc' reports synthesized_footprint — how do I fix it?
applies-to: get/search/put/delete (kind='pcb'); see also kind='part', kind='datasheet'
status: active
tags: [design]
kinds: [pcb]
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
[[precis-measures-help]] (the "measuring tapes"), [[precis-pcb-ewod-help]]
(computed-component **generators** — a `generators: [...]` block on `put`
expands a whole EWOD electrode-array footprint from a few params instead of
hand-authored pads), and the pattern playbooks [[precis-decoupling-help]],
[[precis-i2c-help]], [[precis-spi-help]], [[precis-datasheet-help]].

## Author a design — `put(id=<slug>, args={…})`

**Batch, re-runnable.** One `put` lays down components (with pins), nets, and
connections in one transaction; re-`put`ting the same slug **extends** it
(existing refdes/net names are reused, not duplicated).

A physical pad has one canonical electrical pin name:
conflicting explicit ownership or contradictory same-name pads refuse the
whole put. Use one canonical name in pins/connections or correct the pad.
Existing refdes are reused, not pin-edited/backfilled; conflicting supplied
declarations still refuse on re-put. NULL/unbound pins and repeated physical
footprint pad rows remain legal.

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

A put (and an EasyEDA import) queues a datasheet pull for each C-number the
board uses that has none; the reply says "N datasheet pull(s) queued".
`get(kind='part', id='C…')` shows the datasheet or why the pull failed;
`put(args={'op':'datasheets','force':True})` re-queues failed pulls.

## Author a design — field notes

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
- **net electrical spec** — where datasheet numbers go: `current` (amps →
  IPC-2221 width), `voltage` (peak working volts), `edge_rate` (V/ns),
  `impedance` (ohms), `function` (`crystal`|`switcher_sw`|`adc_input`|
  `digital_logic`|`power_rail` — a fallback when you know what the net
  *does*, not its numbers). **`voltage` is pairwise**: spacing follows
  `|V_a − V_b|` (IPC-2221B), so 48 V beside ground needs room, 48 V beside
  48 V needs none. Both nets must be annotated — a ground net wants an
  explicit `voltage: 0`, since missing is never read as 0 V (`view='drc'`
  names the nets it skipped). These are the only net fields a re-`put`
  patches onto an **existing** net: you import the board, *then* read the
  datasheet.
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
  board per design. That default has only TWO routing layers (both inner
  layers are planes); change it with `put(args={'op':'stackup', ...})` —
  see [[precis-pcb-route-help]]. On a board whose front is spoken for,
  opening an inner signal layer is usually the difference between routing
  and not.
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

### Inspect one connector's actual pads

```python
get(kind='pcb', id='sensor-node#J1', view='pinout')
```

One instance only; the selector is required. The ordinary `#J1` hop stays
the logical pin/net/neighbour view. Pinout reads cached or design-local
physical pads without fetching or changing the board. It shows original
pad IDs, distinct indexed rows for duplicate IDs, local/board mm, side,
rotation, layers and stored pin/net mapping evidence. `explicit-pin-pad`,
`footprint-pin-map` and `pad-number-identity` identify the mapping source;
identity fallback is not an independently known signal. `unconnected`
means a mapped pin without a net; `unclaimed` means no declared pin;
`ambiguous` retains conflicting pins/nets rather than choosing one.

Local +X is right and +Y up; board coordinates are top-view, rotations CW.
Bottom-side pads mirror local X before rotation and translation. Unplaced
instances retain local geometry but have unavailable board coordinates.
Missing cached/authored geometry gives an explicit nextcall, never guessed
pads. Catalog cache source versus authored geometry is stated; neither
verifies the supplier pinout or mating orientation. No provider pull, job,
catalogue refresh, routing or placement runs from this view.

### Preview an unsaved explicit mapping

```python
get(kind='pcb', id='sensor-node#J1', view='pinout-preview',
    args={'pins':[{'name':'CLK','pad':'2'}, {'name':'UNKNOWN','pad':None}]})
```

Existing instance/stored geometry required. This read-only echo adds proposed
labels beside P1's stored evidence; it never writes pins, assigns nets, fetches
geometry or queues jobs. `name` is a semantic label, not a net declaration.
Only `pins` is accepted; each entry requires exactly name/string and
pad/exact-string-or-null. Null means unknown binding; even the label NC does
not establish disconnection. Mating orientation and vendor numbering remain
unknown. Conflicting drafts are shown with canonical-authoring refusal hints,
never selected or persisted. This includes one name with both null and bound
pads; distinct null-bound names stay unknown, not conflicting. `pins=[]`
explicitly means no proposed assignments. Missing geometry stays unavailable with a manual
inspection/authoring hint. Duplicate physical pads remain distinct rows.

Limits: one instance,32 proposed entries,64 physical rows,64-character names,
32-character pad IDs,16384 response characters. Oversize requests return typed
correction, never silent truncation. Ordinary `view='pinout'` reads persisted
evidence only; use that view for larger stored instances.

Every catalog part on the board is also a graph edge: the design `contains`
one part ref per C-number, with the refdes list and qty on the edge, kept
current by each `put`. So `get(kind='part', id='C25804')` lists the boards
that use it ([[precis-part-select-help]]). A C-number not in the catalog gets
no edge.

## See the geometry — `get(view=…)` (the "eyes")

You never look at a render. You ask numeric questions:

```python
get(
    kind="pcb", id="s", view="crossings"
)  # crossed airwires — THE pre-routing objective (planes excluded)
get(kind="pcb", id="s", view="ratsnest")  # the MST airwires + total length (mm)
get(
    kind="pcb", id="s", view="feasibility"
)  # coarse H/V via estimate (NOT real routing) + pins a class "layers" lock strands
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
get(kind="pcb", id="s", view="schematic")  # net-label schematic SVG — works
# before any placement (matching labels ARE the connections; also on the
# web PCB tab)
```

- **crossings** is the objective the placer minimises — fewer crossed wires =
  easier route. **Plane nets** (`gnd/ground/power/pwr/plane`) are excluded from
  the metric (they pour, they don't route point-to-point) but stay fully in the
  netlist.
- **trace** walks series 2-pin parts (a resistor/cap in line) automatically; a
  multi-pin part terminates the auto-walk — you supply the next hop from the
  datasheet ([[precis-datasheet-help]]).
- **Peeking at a rendered board** (`tests/test_pcb_render_fixture.py` → SVG →
  `qlmanage`) is an occasional human confirmation, not the iteration loop —
  the numeric views above are cheaper and are what place/route actually
  score against. Default to a 600px peek in-loop; reserve full 1800px
  (`PRECIS_PCB_PEEK_SIZE`) for milestone/acceptance renders.

## Fill in a missing footprint — `op='footprint'`

`view='drc'` reporting `synthesized_footprint` means a catalog `part=`
instance has no cached pad geometry — it was DRC'd at a fabricated bound,
not its real footprint, so every other finding on that refdes is a guess.
Pull the real one:

```python
get(kind="pcb", id="s", view="drc")  # -> synthesized_footprint: part U1
put(kind="pcb", id="s", args={"op": "footprint", "part": "C639448"})
get(kind="pcb", id="s", view="footprints")  # confirm U1's C-number is cached
```

`parts=[...]` pulls several C-numbers in one call; a failed pull reports
`error` in that part's own row instead of raising, so one bad C-number
doesn't lose the rest. `force=True` re-pulls even when already cached.

The real footprint always wins and is stored. Router copper it now collides
with is ripped (`<net> ripped: … — re-route`); pad or placement collisions
are listed as `now visible (real footprint)` and stand until a re-place. The
cache is shared, so the reply also names other designs using the part.

`pin_name_mismatch` is a different finding: the footprint IS cached, but
a declared pin name matches none of its pads, so that pin still sits at a
synthesized bound. Re-pulling cannot fix it; rename the pin. A design
`put` and `view='footprints'` both list the declared names beside the
footprint's own (`U_TEMP: declared VDD; the footprint names ADD0, ALERT,
GND, SCL, SDA, V+`).

When the vendor has nothing for a C-number, author the footprint directly
instead — same pad shape as `footprints:[...]` in `put`'s design-authoring
args:

```python
put(
    kind="pcb", id="s",
    args={"op": "footprint", "part": "C999999",
          "footprint": {"pads": [{"pin": "1", "shape": "rect", "x": -0.5, "y": 0, "w": 0.6, "h": 0.8}, ...]}},
)
```

`view='footprints'` lists every catalog-part instance on the board
(refdes, C-number, cached?, source, pad count) — the read-side counterpart,
and where to check before trusting a `view='drc'` pass on that part.

## Place and route it — `put(args={'op':'place'|'route', …})`

### The judged-mutation gate

A batch `put`, `op='class_rules'` and `op='footprint'` on a design with placed
parts are checked before and after in one transaction. A design's first put
counts when it carries x/y: its board is checked against an empty "before".
Judged: pads, authored copper, courtyards, holes, the outline. Parts with only
a synthesized (guessed) footprint are not.

- **Refused:** a new or worse error between pads and authored copper stores
  nothing. On a first put any overlap among the authored poses refuses; omit
  x/y and run `op='place'`, or give clear poses.
- **Router copper yields:** any new or worse finding that names routed copper
  rips that net (it becomes unrouted), at any severity, including a class
  requirement tighter than the fab minimum. The design's own rules outrank
  existing routes. The reply lists `<net> ripped: <rule> … re-route`; re-run
  `op='route'`.
- **`op='class_rules'`** never refuses over pads: a shortfall between pads
  against the new requirement is listed as `now visible (class requirement,
  not refused)`.
- **`op='footprint'`** always stores the real footprint; collisions it reveals
  stay as errors until a re-place. Only the calling design is judged; other
  designs using the part get a `check view='drc'` pointer.
- **Standing** findings (present before, not made worse) are only counted.

Placement and routing run as **enqueued worker jobs** — never inline in this
call (a real board is minutes of compute, not milliseconds). `put` returns a
job id immediately; see **[[precis-pcb-route-help]]** for the full `op=` surface
(`place`/`route`, plus the inline edits `move`/`rip`/`pin_side`/`plane_net`/
`class_rules`/`stackup`), the congestion/planes read views, and what's still inert
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
get(kind="pcb", id="s", view="gerber")  # the fab bundle: gerbers + drill, zipped
```

`view='gerber'` runs DRC first and **leads its response with the verdict**.
A board with DRC errors still exports, so you can look at it, but the
response opens with a `DRC FAILED` block naming the error count, the rules
and the first findings. Do not send that bundle to a fab: fix the board and
re-export until the first line reads `DRC: 0 errors`.

`view='route'` runs the §9 hand-off: place → `.dsn` → Freerouting → on an
incomplete route, re-place (more iters) and re-route, bounded. With no router
installed it **degrades to a `.dsn`-only pass** (open it in EasyEDA/KiCad as a
manual escape hatch). `bom`/`cpl` warn about unplaced or non-assemblable
(no-LCSC) parts. An instance that is etched copper rather than a part (a
footprint whose pads are all `role: 'electrode'` or `'probe'`, such as an
EWOD electrode array) is left out of both: there is nothing to place or buy.

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

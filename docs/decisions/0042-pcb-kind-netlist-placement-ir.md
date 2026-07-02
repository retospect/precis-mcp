# 0042 — The `pcb` kind: a netlist + placement IR the LLM can *read*, JLCPCB-native

- **Status**: accepted (2026-06-27) · **v1 partially implemented**
  (2026-07-01: slices 2, 6, 7 landed — parts catalog, exporters +
  Freerouting round-trip, skills; slices 3/8/9 remain — see the
  [implementation tracker](../design/pcb-0042-implementation.md) for
  live slice status). The electronics sibling of
  [ADR 0041](./0041-cad-kind-analytic-ir.md); same philosophy — own a
  legible IR, rent the heavy kernel only at export — applied to circuits +
  boards instead of solids.
- **Implementation tracker**: [`docs/design/pcb-0042-implementation.md`](../design/pcb-0042-implementation.md) (epic + v1 slices).
- **Deciders**: Reto + agent
- **Builds on**:
  - [ADR 0041 — The `cad` kind: analytic-IR solids](./0041-cad-kind-analytic-ir.md)
    — the **direct sibling**. 0041 gives the LLM eyes into a *solid* without
    pixels; 0042 gives it eyes into a *circuit + board* without pixels. The
    **keystone** (own the IR, rent the kernel at export), the
    **persisted-observer/measure** model, and the **probe-ladder** shape carry
    over. The storage **converges** with 0041's **Amendment 1 (2026-06-28)**,
    which moved 0041's nodes to a dedicated `cad_nodes` table + one card chunk:
    0042 does the same (§4), going one step further to a *multi-table*
    normalized schema because a netlist is genuinely relational (it wants SQL
    + FK integrity, not a folded DAG). (0041 not yet on `main`; lands
    alongside.)
  - [ADR 0033 — Drafts as editable chunk-native documents](./0033-draft-chunks-editable-document.md)
    (only the **soft-delete *semantics*** are borrowed — a `retired_at` column
    on the `pcb_*` tables; the design ref keeps one embedded card chunk, but
    the graph is **not** chunk-native, §4).
  - [ADR 0035 — Computed chunks & the recompute boundary](./0035-computed-chunks-recipes-and-the-recompute-boundary.md)
    (the derived layer — ratsnest / crossings / measure verdicts — is
    *recomputed on change*, but **computed-on-read with a memo, not a stored
    chunk cascade**, §12; 0035 is a conceptual, not mechanical, dependency).
  - [ADR 0029 — Multi-root corpus for PDF serving](./0029-multi-root-corpus-pdf.md)
    (exported gerbers / BOM / CPL land on `PRECIS_CORPUS_DIR`; ingested
    datasheets read from it).
  - [ADR 0026 — precis-web as a sibling package](./0026-precis-web-surface.md).
  - [ADR 0036 — Universal handles](./0036-universal-handles.md) (the `pcb`
    2-char code is **`pb`**, the parts-catalog `part` is **`pn`**, the
    `datasheet` kind is **`da`**).
  - **Paper ingest + the `cfp` precedent** (`ingest/{marker,pipeline,
    text_chunker}.py`, `handlers/cfp.py`) — datasheets are a **third thin
    `PaperHandler` sibling** (`kind='datasheet'`, §7.1) over the *identical*
    Marker→chunks pipeline, so they get embed / keywords / TOC / search /
    two-pane reader for free, exactly as `cfp` did over `paper`.

## Context

We want an electronics-CAD capability: an LLM designs a circuit, picks
real manufacturable parts, places them, and ships a board to JLCPCB. The
hard question is the same one 0041 answered for solids — **how to give an
LLM *eyes* into a circuit and a board without making it stare at pixels.**

A rendered schematic or a copper-layer raster is the obvious answer and
the wrong one, for the same reasons as 0041 (needs a GL/render context, is
lossy, costs tokens) plus a sharper one: **a circuit *is already* a graph**
and **a board *is already* a set of placed rectangles with a connectivity
overlay.** Both are natively structural. An LLM reasons about "U3 has 6
decoupling caps, 2 of them are >8 mm from a power pin, and net `SCL` crosses
4 other nets" far more reliably — and far more cheaply — from the structure
than from a picture of green traces.

So the precis-shaped answer, exactly as for papers and solids: **structure
the model can query** — a netlist TOC, targeted net/component lookups, and
*probes* that compute connectivity, crossings, proximity, and rule-checks
**analytically over the graph and the placement** — never a routed-copper
image, never a SPICE run, to *inspect*. The heavy machinery (autorouting,
copper pour, fabrication) happens only at **export**.

### One important factual correction to the premise

JLCPCB is a **fab + assembly house**, not an autorouter. It fabricates
from **gerbers** and assembles from a **BOM + CPL (pick-and-place)**; it
does **not** route a board from a bare netlist. The "ship it to JLCPCB who
do the final routing" step is really two different vendors of the same
company: **EasyEDA** (JLCPCB's free EDA tool — knows the JLCPCB/LCSC part
library, has an autorouter, and one-clicks to a JLCPCB order) does the
*routing*; JLCPCB does the *fabrication*. The open-source equivalent is
**KiCad + Freerouting** (Freerouting consumes a Specctra `.dsn`, returns a
routed `.ses`) → KiCad gerbers → JLCPCB. Either way **we hand off a
*placed netlist*, the routing engine fills in copper, the fab makes it.**
This is the EDA analog of 0041's "OpenSCAD/OCCT are export backends, not
the evaluator." (§1, §13.)

## Decision

### 1. Keystone — own the netlist + part-selection + placement IR; rent the autorouter + fab at export

The IR — **parts + nets** (the logical layer) and **placement + layer
assignment** (the physical layer) — is the source of truth and the thing
the LLM authors, reads, and probes. **The autorouter (Freerouting /
EasyEDA), the gerber generator (kicad-cli), and the fab (JLCPCB) are export
backends**, not the evaluator and not the store. What we own and the LLM
sees: *which parts, wired how, placed where, with how many crossings left
to route.* What we rent: turning the pre-minimized placement into actual
copper, and turning copper into a board.

The cost we accept: a small connectivity-and-geometry evaluator of our own
(§9) plus a parts-catalog mirror (§5). The discipline that bounds it:
**model only what we can compute exactly or label as an estimate** (§11)
— the same contract-as-exclusion-line move 0041 uses for its membership
card.

### 2. Two layers, cleanly separated — logical vs physical

A `pcb` design carries two layers over one node set, mirroring 0041's split
of the *analytic graph* from the *evaluated geometry*:

- **Logical** (the schematic *intent*, fab-agnostic): **components** (part
  instances) + **pins** + **nets**. This is what "the circuit is" — it
  exports to a netlist and is what SPICE/simulation would consume. **No
  symbol graphics** — we keep the connectivity, not a drawn schematic
  (pixels out, exactly as 0041 emits solids not drawings).
- **Physical** (the board): each component's **placement** — `(x, y,
  rotation, layer)` — plus the **layer assignment / route estimate**. This
  is *derived and editable* over the logical netlist; re-running placement
  never touches the netlist.

The separation is load-bearing: the LLM can get the circuit *right* (the
netlist) before it gets the board *good* (the placement), and a placement
edit can never silently corrupt connectivity.

### 3. Invariants

- **Units: millimetres, `float64`** (same rationale as 0041 — the
  EDA/gerber/JLCPCB lingua franca is mm; mils accepted as input alias for
  trace widths since datasheets quote both, stored as mm).
- **2.5D, rigid.** Placement is a 2D pose `(x, y, θ)` on a discrete
  **layer** (top / bottom in v1; inner layers are planes, §10). Rotation
  is free (0–360°, snapped to the footprint's allowed steps where the part
  declares them). **No scale, no shear** — a footprint's courtyard is its
  fixed real size from the JLCPCB footprint data.
- **No grid.** Continuous placement (the user's explicit ask). A grid is a
  routing convenience we don't need to *place* or to *count crossings*;
  the autorouter imposes its own at export.
- **The logical netlist is the invariant; the placement is the variable.**
- **Coordinate frame (decided 2026-06-28).** One mm frame per design; **origin
  (0,0) at the board-outline reference corner**, **+X right, +Y up (north)**.
  An instance's `(x,y)` is its **footprint centroid** (the pick-&-place pickup
  point); **rotation pivots about that centroid**. **Rotation = degrees
  clockwise from north (+Y), positive** — our *internal* convention;
  **exporters convert** (KiCad/gerber are CCW-from-+X; the **JLCPCB CPL
  rotation** needs a per-package fix map). **Bottom-layer instances mirror in
  X.**

### 4. The node model — a relational graph, *not* a chunk set (converging with 0041 Amendment 1)

0041's v2 originally stored its design as **chunks under a ref**, but its
**Amendment 1 (2026-06-28)** moved the nodes to a dedicated `cad_nodes`
table, keeping **one** `card_combined` chunk for intent-search. 0042 lands
on the *same* shape — **dedicated table(s) + one card chunk** — so this is
**convergence, not divergence.** The shared reasoning:

1. **We do not want the netlist in the corpus.** The single strongest reason
   to use chunks — free embed / semantic-search / TOC / two-pane reader —
   does not apply: a connectivity graph is not something you embed and
   retrieve. With corpus-membership off the table, chunks lose their payoff
   (the same realization that drove 0041's amendment).
2. **The data is a graph we must *query*.** A net is an N-ary hyperedge over
   pins; "which nets touch U3?", "fanout of SCL", "every `noisy` net running
   parallel to a `sensitive` net" are SQL joins / aggregates with **foreign-
   key integrity** (a dangling pin or net reference is a *bug* a FK prevents
   for free). Over chunk-JSON those are hand-rolled traversals with no
   integrity guarantee, and every evaluator pass (crossings, fold, DRC) would
   re-parse JSONB instead of reading rows.

The **one way 0042 goes further than 0041's amendment**: 0041 needs a single
flat `cad_nodes` table (its DAG is one node list); a netlist is genuinely
*multi-table relational* (components+pins / instances / nets / a `pcb_netconns` join /
measures), so 0042 normalizes into several FK-linked tables (§12) rather than
one node table. Either way, **only one thing is in the corpus: the design
itself** — a `refs` row (`kind='pcb'`) carrying **exactly one embedded
`card_combined` chunk** ("I²C sensor node, ESP32-C3, 42 parts") — so the
*board* is findable by intent and gets a `pb` handle, soft-delete, and links
(`datasheet-of`, `produced-by`, `has-requirement`). Everything below is rows,
soft-deletable via a `retired_at` column (the 0033 *semantics* + the `cad_nodes` precedent, without the
chunk *mechanism*). Same keystone as 0041 (own the legible IR, rent the
kernel at export). The node *concepts* are unchanged:

- **`component`** — one physical part instance (a `pcb_components` row): a
  **refdes** (`U3`, `C12`),
  a **value/function**, the chosen **`part` handle** (the LCSC part, §5),
  its **footprint** + courtyard rectangle, and (physical layer) its
  **placement pose** + a **`fixed` mark** (§4.1). A component *auto-exposes
  its pins* as named datums (`U3.SCL`, `C12.1`) — exactly 0041's "a cylinder
  exposes its axis." Pins come from the footprint / datasheet pinout.
- **`feature`** — a **non-electrical placed row** (`pcb_features`): a
  mounting / **screw hole**, a fiducial, a test point, a board-edge keepout,
  the **board outline** itself. It has a pose and (usually) a `fixed` mark,
  takes part in courtyard/keepout checks and the mechanical exporters (§13),
  but has no pins and joins no net. (A user-facing **status LED** is an
  ordinary `component` that happens to be `fixed` — it *is* on a net; a screw
  hole is a `feature` — it is not.)
- **`net`** — **an N-ary hyperedge over pins** (name, class, current). This is
  the answer to *"is the netlist links or chunks?"* → **neither**: a
  `pcb_nets` row plus a `pcb_netconns` table with **one row per
  (net, instance, pin)** (the netlist triple, §12 + the proposal). A net is
  N-ary (a power net touches 40 pins), so binary links would explode into N²;
  the triple is the textbook shape and lets `pcb_nets` carry per-net facts
  (class, current, width, layer hint) with FK integrity.
  *(Earlier drafts folded the pin into the edge as a `pin_name` string and had
  no `pcb_pins`; the design discussion corrected this — see the type/instance
  split below: pins are first-class rows on the component **type**, and
  `pcb_netconns` references `(net, instance, pin)`.)*
- **`component` (type) / `instance` / `pin` — the normalized split.** A
  **component** is a part *type* used in this design and **owns its pins**
  (`pcb_pins`: pad + function `name` + electrical `tags[]`); a **component
  instance** (`pcb_instances`) is a *placement* (`U3`) with coordinates /
  layer / `fixed` / `roles`. So a `pcb_netconns` row ties a **net** to a
  **(instance, pin)** — and a *physical* pin is on **at most one net**.
  Repeated parts (every passive) define the type once + many instances; the
  BOM is "count instances per component." Composite FKs force a netconn's pin
  and instance to share a component. (Full DDL: the proposal.)
- **`measure` (the "soft relationships," on a hard↔soft spectrum)** —
  **the single row family that carries every design *intent*** the user
  named, from "this cap is near that chip because it's a bypass" and "this
  terminator is distal" to "this sensitive opamp must stay *far* from that
  switching FET." One `pcb_measures` row covers all of it: a **relation** (the
  measure kind — §8.3 library), its **operands** (a small JSONB list of
  component / net / class references — heterogeneous, read by the evaluator,
  not heavily joined), a **direction of goodness** (`min` / `max` / `target`
  / `keep-above` / `keep-below`), an optional **goal** (`≤3 mm`, `≥10 mm`),
  a **strength** (`hard` = a DRC rule that *must* hold · `soft` = a weighted
  term the placer optimizes · `gauge` = report-only measuring tape), and a
  **reason** (free text — the intent). It is **persisted and re-evaluated**:
  it reports its current value + verdict after every placement pass and
  "tries again." This *is* 0041's persisted relational-observer model
  (clearance / dof), generalized — a clearance check is just a
  `min`-direction `hard` measure. (A **row, not a bare link**: a link can say
  *that* the cap bypasses the chip; a row also carries the *gap target*, the
  *direction*, the *strength*, and the *violated?* verdict, and re-evaluates
  — the measure is active geometry, not a static fact.) The placer reads
  `hard`+`soft`; the eyes (§8) report all three.
- **`observer`** — a *saved query* pinned to the design (a crossing-count
  watch, a DRC-lite ruleset run, a saved signal trace) — the lighter
  sibling of a `measure` (no goodness direction, just "recompute and show
  me this"). A `pcb_measures` row with `strength='gauge'` and no goal is the
  degenerate form, so observers need no separate table. 0041 §7's observers,
  in spirit.

**Role/class tags carry intent without naming every part.** Components are
tagged by **electrical role** — `sensitive` / `noisy` / `high-speed` /
`analog` / `power` / `hot` / `temp-sensitive` / `connector` — which the LLM
assigns from datasheet reasoning (§7). Measures then select by *class*
("keep `hot` away from `temp-sensitive`," "no `noisy` net parallel to a
`sensitive` net") instead of enumerating pairs — so a 40-part board needs a
handful of measures, not hundreds. Net classes (§8 DRC, Q3) work the same
way.

**Addressing.** The **design** gets a `pb…` universal handle (ADR 0036 — a
normal refs-backed kind). Sub-rows are addressed by their **natural EDA
identity scoped to the design**, not a global per-row handle: a component is
`pb12#U3`, a pin `pb12#U3.SCL` (or `#U3.7`), a net `pb12@SCL`. Refdes and
net-name *are* the identity an engineer uses, they read better than a
synthetic id, and they survive row churn — so we deliberately do **not** mint
a 0036 handle per component/net (though 0036 could resolve one to the new
tables if we ever want it; see Open questions).

#### 4.1 `fixed` — some things don't move

Not everything is the placer's to optimize. A **`fixed` mark** on a
`component` or `feature` pins its pose (position + rotation, and optionally
just one of them — `fixed-xy`, `fixed-rot`): **screw / mounting holes**, the
**board outline**, board-edge **connectors**, a **user-facing status LED**
(it must sit where the enclosure window is), test points, fiducials. The
**placer (§9) and the shove router (§13a) treat a `fixed` node as an
immovable obstacle** — they route *around* it and shove *other* things, but
never it. `fixed` is therefore the human/LLM's primary steering lever over
an otherwise-automatic layout: mark the handful of things the physical world
dictates, let everything else float. The eyes render fixed nodes with a 📌
mark so "what's locked" is always legible; a placement report leads with the
fixed set.

### 5. The parts catalog — `part` kind, JLCPCB-native selection

Manufacturability is a **construction-time invariant, not a post-hoc
check** (the user's hard requirement: *only* parts with JLCPCB-compliant
footprints; default to the most-in-stock).

- **`kind='part'` (`pn`)** — the **`parts` catalog table** (§12) mirroring
  the JLCPCB **assembly parts catalog** joined to **LCSC** stock/price. One
  row per orderable part, keyed by LCSC C-number. Fields: `lcsc` (`C25804`), `mfr_part`, `description`,
  **`jlcpcb_assemblable`** (bool — has a JLCPCB-validated assembly
  footprint), **`basic`** (Basic vs Extended — Basic has no per-type
  feeder/loading fee), **`stock`** (qty), `price` (qty breaks),
  `package`/`footprint`, **package height** (mm — feeds the `height/access`
  measure §8.3 and the component-block export §13), parametrics
  (`{capacitance, voltage, dielectric, tolerance, …}`), `datasheet_url`.
  `corpus_role='none'`; **acquired by a refresh worker, not by `put`** (like
  `cfp`/`paper` are ingest-only).
- **Sourcing** (the user's *"can we get it directly from LCSC?"*): **both
  vendors, because they answer different questions.** LCSC's API gives the
  *catalog + live stock + price*; the **JLCPCB-assemblable flag + the
  assembly footprint** is the *JLCPCB* layer (JLCPCB publishes its assembly
  parts list; community mirrors exist). So the catalog is **LCSC for stock,
  JLCPCB for "can it be assembled."** Where parametrics or datasheet links
  are thin, **Octopart/Nexar** backfills structured cross-distributor data.
  Use the **official feeds**, and **process them heavily before the LLM ever
  sees a row** (normalize parametrics, derive height, resolve footprint, fold
  in the assemblable flag) — the LLM picks from a clean, typed catalog, never
  from raw vendor JSON. A `parts_refresh` worker (system profile) pulls
  deltas on a cadence; `stock` is the volatile field, re-checked at selection
  time. **Concrete v1 pipeline (decided 2026-06-28):** the **footprint
  geometry + pin-name→pad map + 3D** come from **`easyeda2kicad`** (pulls LCSC
  component data, converts to KiCad), and the `jlcpcb_assemblable` /
  Basic-or-Extended / stock fields from the **downloadable JLCPCB assembly
  parts CSV** — no paid LCSC API assumed. Footprint+pin resolution is the
  catalog's hardest part, so it is its own ingest step, not a field.
- **Two data flows, kept separate (decided 2026-06-28).**
  - **Flow A — the bulk catalog (cheap full-list slurp).** Source = the
    community **`jlcparts`** dump (the whole JLCPCB catalog with stock + price
    as a daily SQLite/JSON), *not* self-scraping. The `parts_refresh` worker
    ingests it via **staging-table + atomic swap** (`COPY` the dump into
    `parts_staging`, build indexes there, then `DROP parts; ALTER … RENAME` in
    one txn) — or truncate+`COPY`-in-txn for the simple version. The
    drop-index → `COPY` → recreate-index trick is a ~2× lever, optional at our
    row count (~100–300k assemblable parts), add it only if load time bites.
  - **Flow B — footprints/pins (lazy, expensive, never bulk).** The
    `easyeda2kicad` conversion is per-part and slow, so it is **lazy**: run on
    first *selection* of a part and **cached in a separate `part_footprints`
    table keyed by C-number**. The catalog swap/reload never touches it —
    which is *the* reason it is its own table. (Don't convert all ~100k+
    parts; only the few a design uses.)
  - **Cadence.** Because stock is **live at selection** (above), the daily-
    stale dump only ranks/filters candidates → the bulk refresh can run daily
    (matching `jlcparts`) or weekly; freshness doesn't depend on it.
- **The selector verb** — `search(kind='part', q='0.1µF 0402 X7R ≥16V')`
  (or a structured spec). **Hard filter:** `jlcpcb_assemblable = true AND
  footprint IS NOT NULL`. **Default rank:** `basic DESC, stock DESC, price
  ASC` — i.e. *prefer a Basic part, then the most in stock, then cheapest*
  (the user's "most in stock by default," with Basic-first because an
  Extended part adds a per-type fee that dominates at low qty). Returns
  TOON candidate rows so the LLM picks with the numbers in front of it.
  Choosing a part *stamps the `part` handle onto the `component` node* — the
  component now carries a real footprint + courtyard, so placement and BOM
  are exact.

### 6. The BOM is a view, not a kind

A bill of materials is **derived from the design**, so it's a `get(kind=
'pcb', view='bom')` view, not a stored kind (the parallel to 0041's
section/clearance being views, not objects). It groups components by
`part`, and emits the JLCPCB-assembly-ready columns:

```
# board "sensor-node" BOM — 18 lines · 42 parts · 16 Basic / 2 Extended · est $3.91/bd @100
{refdes        qty  part    value      footprint  basic  stock    $ea}
C1,C3,C5,C7    4    pn812   100nF      0402       yes    1.2M     0.0008
U3             1    pn4471  ESP32-C3   QFN-32     no     8.4k     0.61
R7             1    pn220   4.7k       0402       yes    980k     0.0011
…
```

Export of this view (§13) is literally the JLCPCB **BOM CSV**; the
placement view exports the **CPL/pick-and-place CSV**. The same `view=
'bom', mode='wordcount'`-style affordance can flag *over/under stock* or
*Extended-part* lines as a web badge, the way `cfp` flags word limits.

### 7. Datasheets — a thin `paper`-family kind, deliberately capped

A datasheet is a PDF; the corpus already ingests PDFs beautifully. So a
datasheet is **ingested through the identical Marker→chunks pipeline**
(embed / keywords / TOC / search / two-pane reader for free), and the LLM
*reads* it — pinout, typical application circuit, decoupling/bypass guidance,
bus timing — and emits **components, nets, and measures** that follow it.
(This is the whole point: the datasheet says "place a 100 nF cap within 3 mm
of each VDD pin" → the LLM mints a `bypass-of` `proximity` measure with
`goal ≤3 mm`.)

#### 7.1 Is this sprawl? — the kind-taxonomy call

`cfp` is a ~30-line `PaperHandler` subclass that differs from `paper` *only*
by a `KindSpec` declaration (`corpus_role='spec'`, no `put`, custom views) —
the whole ingest / storage / reader / search engine is shared. A datasheet
is the same move, so the worry is fair: are we breeding `paper` clones? Two
rulings keep it bounded:

- **`datasheet` is its own kind** (`da`), *not* `kind='paper'` + a tag — for
  three concrete reasons, and only these:
  1. **Search scoping** — 10k component datasheets must not pollute academic
     `search(kind='paper')`, nor vice versa. Kind is the clean scope; a
     tag-filter is the hack.
  2. **Part linkage** — `datasheet-of` / `has-datasheet` joins it to `parts`
     rows; one datasheet often covers a part *family* (many-to-many).
  3. **Ingest-only + lazy** — `supports_put=False` (like cfp), and acquired
     from the catalog's `datasheet_url` **on demand** (when the LLM opens it
     or a design references the part), not eagerly — so the corpus never
     fills with datasheets for parts you never place.
  It keeps `corpus_role='evidence'` (a datasheet *is* citable, unlike a cfp),
  so it is even thinner than `cfp`.
- **One kind for the whole electronics-doc *family*, not one per genre.**
  `datasheet` also carries app-notes / errata / reference-manuals,
  distinguished by a `meta` sub-type if ever needed. **The cap is explicit:
  the axis is `corpus_role` + a meta sub-type, never a new kind per document
  genre** — we will not mint `appnote`, `errata`, … So the catalog grows by
  *one* thin sibling, the machinery by *zero*.

So: paper (academic evidence) · cfp (spec) · datasheet (vendor evidence) —
three declarative siblings over one pipeline, and the line is drawn here.

#### 7.2 Two known ingest gaps (sub-tasks, not blockers)

- **Table recognition.** Pinout tables and electrical-characteristics
  tables are denser and more grid-like than paper tables; Marker's table
  extraction needs tuning (or a datasheet-specific pass) so a pinout
  survives as structure, not prose. Tracked as an ingest improvement.
- **Non-PDF exchange.** There is no universal structured datasheet format,
  but **Octopart/Nexar** (and some vendors' HTML datasheets / IPC-2581 part
  models) carry structured parametrics + pinouts. Where available, prefer
  the structured source over OCR'ing a PDF; PDF is the fallback, not the
  only road.

### 8. The eyes — probes, signal traces, and measures

This is the **real** 0041 parallel (the user's clarification): not
"render the board and interpret the image," but **probing mechanisms** that
let an LLM *see what is far from what, what is near what, and how the signal
flows* — directly over the graph + placement, as numbers. Three families,
increasingly soft.

#### 8.1 Probes — exact, mechanical queries (the ratsnest ladder)

> **A probe is a query against the graph or the placement; the answer is
> exact geometry or a labelled estimate.** (0041's probe ladder, retargeted
> from "material vs void along a ray" to "connectivity and crossings.")

The **ratsnest** (airwires — straight pin-to-pin lines for every unrouted
net) *is* the LLM's "tracer lines":

- **TOC — the netlist.** Components + nets + fanout + the measure set; one
  line per net (a 40-pin power net is one row).
- **Net probe ("see the net").** Pins, ratsnest segments, total length,
  **which other nets it crosses**, layer, net class, estimated current.
- **Crossing probe ("see the crossings").** The **global ratsnest crossing
  count** — *the* pre-routing objective (§9), exact (airwire-airwire
  intersection), watched dropping pass over pass.
- **Proximity probe.** Distance between two components / pins.
- **Density / congestion probe.** Bbox, courtyard-vs-board utilization, a
  coarse congestion grid, **estimated routing channels** (a *labelled
  estimate*, §11).
- **DRC-lite probe.** Hard rules the LLM sees *before* the autorouter:
  unconnected pins, bypass-cap presence per power pin,
  **trace-width-for-current**, **broken/split-plane return-path** (§10),
  power/ground sanity, courtyard overlap.

#### 8.2 Logical signal trace — "follow the signal" (the primary read surface)

**This is how the LLM authors and reasons (decided 2026-06-28): the netlist
is records traversed as a graph, not a blob it writes.** The flow is `get`
IC4 → its pins → the net on each pin → the components on that net → reason →
hop onward — and edits are `put`/`edit` on individual rows. So graph
navigation is the *load-bearing* surface, not one probe among many; the
handler must expose a cheap **`get(neighbors)`-style hop** from any
pin / net / component (one row → its adjacent rows), and the trace below is
that hop, iterated.

The user's *"I²C comes out here, to the mux, to the other bus, then to that
component"* is a **graph walk, not a placement query.** A **trace probe**
starts at a pin/net and walks the netlist, **hopping through a component via
its declared port pass-throughs** (a mux's `A→B`, a series resistor's
`1↔2`, a level-shifter's `LV↔HV`) — where a part doesn't declare its
internal topology, the LLM supplies the hop. It returns the **ordered
logical path** (net → pin → component → net …), with branch points flagged.
This is how the LLM reasons about a bus end-to-end ("the SCL line fans out
to 3 sensors after the mux") without ever placing anything — pure
connectivity, exact. (No netlist-import / DSL in v1; either could be added
later as sugar over these same rows.)

#### 8.3 Measures — the measuring tapes (graded design intent)

The heart of the request: the **"vague" things a design engineer keeps in
their head, made trivially checkable.** A **measure** (§4) is a *named
metric over the graph+placement, with a direction of goodness* — a tape
measure stretched between things, reporting its current value, its verdict,
and (for the human SVG) drawn as an annotated line. Each is a **small pure
function**; adding one is a single library entry (the extensibility surface,
like 0041's primitive set). The **library** the user sketched:

- **`separation`** (`max`) — keep A far from B: *sensitive opamp ↔
  switching FET*, *hot ↔ temp-sensitive*. Reports the current gap and
  whether it's the binding pair in its class (`max(min-over-class-pairs)`).
- **`proximity`** (`min`) — keep A near B: *bypass cap ↔ its power pin*,
  *terminator at the bus end*.
- **`adjacency / parallelism`** — *"is a noisy line next to a silent
  line?"* The **parallel-run length and spacing** between two nets' airwires
  (or routed segments) — the crosstalk tape. Reports the worst offending
  parallel run between a `noisy` and a `sensitive` net.
- **`supply-path`** — *"from the VCC of the sensitive part, how far to the
  VCC of the noisy part — and does it pass the source?"* The **electrical
  path length along a power net's tree** from a load back to the regulator /
  bulk-decoupling, and **whether two loads share a path segment before the
  source** (shared-impedance / ground-bounce reasoning). This is path
  *through the net topology*, not straight-line distance.
- **`distribution-topology`** — *"is it a tree / star?"* Classify a net's
  shape (**star** preferred for power, **daisy-chain** for some buses,
  arbitrary tree) and report it; a star-power measure flags daisy-chained
  supplies.
- **`plane-continuity`** — *"good ground planes, or segmented massively?"*
  A **graded** version of the broken-plane DRC: how fragmented the GND/PWR
  pour is under a net/component, and whether a net's **return path crosses a
  plane split** (the return current detours). Plane nets are treated
  *as planes*, not airwires — the relevant tape is via-to-plane count and
  split-crossings, not pin-to-pin length (the user's "planes can be wide").
- **`height / access`** — *"tall parts surrounding a tiny part is hard to
  fix."* For each component, are its neighbours within R **taller**
  (blocking rework, hand-soldering, airflow, or a connector mate)? Uses
  **package height** from the catalog (§5). A `height-island` flag is the
  trivially-checkable form.
- **`thermal`** — *hot ↔ temp-sensitive* separation (a `separation` keyed on
  the `hot`/`temp-sensitive` classes) and "is a `temp-sensitive` part
  downwind/adjacent to a `hot` one."
- **`length-match`** (phase 2) — diff-pair / bus skew (`target`).

Every measure has a **strength** (§4): `gauge` = just show the tape; `soft`
= a weighted term the placer pushes on; `hard` = a DRC rule. So the same
"sensitive↔noisy" intent can start as a *gauge* the LLM reads, get promoted
to a *soft* objective once it matters, and become *hard* if it's
non-negotiable — without changing its shape.

Out of scope for the eyes (export / simulate elsewhere): **routed-copper
rendering, SPICE/analog simulation, signal-integrity field solving.** A
**section SVG of the ratsnest + the active measuring tapes** (exact
straight-line geometry, not a render) is the primary web view — the analog
of 0041's exact section SVGs.

### 9. Auto-kinda-place — continuous placement, crossing-minimizing

No grid. Each component has a continuous pose `(x, y, θ, layer)`. Placement
is an **optimization**, not a declaration:

- **Objective:** weighted sum of **(a)** ratsnest crossing count (§8),
  **(b)** total ratsnest length, and **(c)** every `soft` **measure** (§8.3
  — separation, proximity, plane-continuity, height-island, …), **subject
  to** courtyard non-overlap and the `hard` measures / constraints. The
  measures *are* the soft objective — "shove the sensitive part away from
  the FET" is a `soft separation` term with a weight, not special-case code.
- **Method:** a **multipass** scheme — force-directed seed (springs along
  airwires, repulsion between courtyards) → **simulated annealing /
  iterative refinement** that *shifts and rotates* components to shed
  crossings (the user's "multipass place shifting things around"). Rotation
  matters: flipping a 2-pin part or rotating a QFP 90° often un-crosses a
  bus for free. Each pass **`log()`s the objective** (crossings, length) so
  the LLM watches it converge, **`fixed` nodes (§4.1) never move** (screw
  holes, edge connector, status LED), and the LLM can mark more `fixed`,
  tweak measures, and re-run. This is "auto-*kinda*-place": good enough to
  hand a router a head start, with the LLM in the loop on the parts that
  matter.
- **"Auto-kinda-route" = a *feasibility estimate*, not copper.** A coarse
  pass assigns each ratsnest segment to a signal layer under an **H/V
  Manhattan model** (horizontal segments on one signal layer, vertical on
  the other — the classic two-layer discipline, §10), counts **residual
  same-layer crossings** (→ vias) and **estimates via count + channel
  congestion**. This tells the LLM (and the human) *"this placement is
  routable on 4 layers with ~30 vias"* **without laying a single track.**
  Real copper is the autorouter's job at export (§13).
- **Round-trip place↔route (the Xilinx move).** Placement and routing are
  *one loop*, not a one-way handoff (the user: at Xilinx, when the router
  exhausts, ship it back to the placer to shove parts around and make room).
  So: place cheap → **rent the router** (§13) → **if the router reports
  congestion / unroutable nets / exhaustion, feed that failure region back
  to the placer** as extra congestion-relief weight and re-place *just the
  hot area* (pin everything that routed cleanly), then re-route. Bounded
  passes. We deliberately **place lightly first and only place *harder* when
  the rented router actually fails** — no point out-optimizing a router that
  would have finished anyway. The LLM is the loop's pilot: it reads the
  router's congestion report (§8 congestion probe) and decides what to shove
  (and can do it by hand — `near`/`distal`/`keepout` measures — where its
  circuit judgment beats the annealer). §13 carries the same loop to its
  logical conclusion (owning the router).

### 10. Layers / stackup / planes

- **Default 4-layer: `Sig-top / GND / PWR / Sig-bottom`** (decided
  2026-06-28) — components + their escape routing on the **outer** layers,
  the two solid planes **inner**. This is the SI-conventional order *and* the
  component-friendly one: SMD parts mount on the top surface, so the top
  layer needs pads + routing and **cannot** be a solid power plane (you'd
  shred it around every pad). A power-outer / ground-outer "stripline" stack
  (signals inner, shielded) is a real technique but only works when
  components don't live on a plane layer — out of scope for v1.
- **The H/V routing model is the simplification we keep.** The two signal
  layers are biased **horizontal** and **vertical** (vias to switch) — the
  §9 Manhattan estimate. Note this is a *routing discipline over the two
  signal layers*, distinct from the stackup order itself.
- **Planes are conductors.** GND/PWR pours on the inner layers make
  decoupling return paths and power distribution "free" (a pin drops to its
  plane through a via). The pour itself is generated at export / by the
  autorouter, not stored — but its *consequences* are visible: a
  **broken/split plane** (a slot or a competing pour that forces a return
  current to detour) is a **DRC-lite warning** (§8, return-path
  discontinuity). So "broken fill planes" are modeled as a *rule the LLM
  sees*, not as drawn copper.
- **2-layer** supported (e.g. GND-bottom / signal+VCC-top) for trivial/cheap
  boards. **1-layer (aluminum / MCPCB) is a future goal** — single-layer
  forces a **jumper (0 Ω / resistor) for every crossing**, so you must
  *minimize crossings and untangle the netlist first*. That is exactly what
  the §9 crossing-minimizing placer does, so it **does double duty**:
  pre-routing optimization now, jumper-minimization for 1-layer later. (The
  placer's crossing count becomes the jumper count.)

### 11. Exact vs estimated

Under the rigid 2.5D model, the load-bearing quantities are **exact**:
connectivity, ratsnest length, **crossing count**, pin/component proximity,
courtyard overlap, constraint satisfaction. **Estimated** (and always
labelled as such, per 0041 §8): **routability, via count, channel
congestion** — these are the autorouter's true domain and we only
approximate them to guide placement. Anything needing copper, fields, or
time-domain behavior (impedance, EMI, SPICE) is **out** of the design loop
entirely.

### 12. Storage & evaluation — dedicated tables

> Full reviewable DDL: [`docs/design/pcb-schema-proposal.md`](../design/pcb-schema-proposal.md)
> (the proposed `0042_pcb_kind.sql`). Sketch below.

The design is a `refs` row (`kind='pcb'`, one embedded card chunk, §4); the
graph is **normalized tables keyed on `ref_id`**, each with a `retired_at`
for recoverable soft-delete:

> **Full DDL: [`docs/design/pcb-schema-proposal.md`](../design/pcb-schema-proposal.md) (v2).** Sketch:

```
refs (kind='pcb')             -- the design; meta = stackup+copper, width-table, board size, rev, best-placement; one card chunk → search + pb handle
pcb_components(id, ref_id→refs, label, part_lcsc, footprint, courtyard, centroid, height_mm, note, retired_at)   -- the component TYPE (owns pins)
pcb_pins      (id, component_id→pcb_components, pad, name, tags text[], description, note, retired_at)            -- pins of the type
pcb_instances (id, ref_id→refs, component_id→pcb_components, refdes, x, y, rot, layer, fixed, roles text[], note, retired_at)   -- a placement (U3)
pcb_nets      (id, ref_id→refs, name, net_class, est_current_a, width_mm, note, retired_at)                      -- name REQUIRED + meaningful
pcb_netconns  (id, net_id→pcb_nets, instance_id, pin_id, component_id, note)   -- netlist triple (net,instance,pin); composite FKs; UNIQUE(instance,pin)
pcb_measures  (id, ref_id→refs, metric, direction, goal, strength, weight, operands jsonb, reason, retired_at)
pcb_features  (id, ref_id→refs, ftype, x, y, rot, layer, fixed, geom, note, retired_at)   -- screw holes, outline, fiducials, keepouts
parts         (lcsc PK, mfr_part, desc, jlcpcb_assemblable, basic, stock, price, package, height_mm, params, datasheet_url, description_tsv)   -- Flow A bulk; staging-swap; NO inbound FK
part_footprints(lcsc PK, pads, pin_map, courtyard, centroid, kicad_mod, …)     -- Flow B lazy easyeda2kicad cache (survives swap)
part_availability(lcsc PK, stock_now, ewma_stock, restock_count, trend, discontinued, …)   -- turnover signal; selection ranks on this, not live stock
```

- **The derived layer is *not* stored** — ratsnest, crossings, measure
  verdicts, the placement objective are **computed-on-read, memoized per
  `(ref_id, rev)`** (rev in `refs.meta`, bumped on any graph write), not a
  table; **plane (gnd/power) nets are excluded from the ratsnest/crossing
  metric** (they drop to the plane). No 0035 chunk cascade.
- **Indexes** that matter: `pcb_netconns(net_id)` + `pcb_netconns(instance_id)`
  (the graph hop), `pcb_pins(component_id)`, `pcb_nets(ref_id,name)`,
  `pcb_instances(ref_id,refdes)`, GIN on `roles`/`tags`. **Composite FKs**
  `(instance_id,component_id)`/`(pin_id,component_id)` force a netconn's pin +
  instance to share a component. Partial indexes exclude `retired_at`.
- **`note` on every authored row** stores the LLM's reasoning (the *why* of a
  wire/placement); inline, not embedded.
- **Crossing count** is O(segments²) with an **AABB pre-filter** over airwire
  bboxes (most pairs are far apart); net fold is O(pins). At hobby/prototype
  sizes (tens–low-hundreds of parts) this is instant; **`log()` any cap** so
  "checked the whole board" never silently means "the first K nets" (0041
  §9's discipline). **No spatial index** until boards get big.
- **Concurrency** is row-level (`FOR UPDATE` on the touched rows), not a
  chunk-set rewrite — cleaner for two ticks editing one design.
- **Forward-only migrations** (the repo rule): one migration adds the `pcb_*`
  + `parts` tables; the handle code `pb` is registered for the `pcb` ref.
- **Plugin vs in-tree:** the tables + handler + workers are **in-tree**
  (§14), so the migration ships in the main chain.

### 13. Cadence & export

**Many small exporters off one IR** (the user's "different exporters"), not
one monolith — each emits one artifact from the same node set:

- **Logical → netlist + assembly inputs.** A **netlist** (KiCad netlist
  and/or **Specctra `.dsn`** with placement baked in), the **BOM CSV** (§6),
  the **CPL / pick-and-place CSV** — the JLCPCB assembly trio is *gerbers +
  BOM + CPL*.
- **Physical → routed board.** Hand the **placed `.dsn` to Freerouting**
  (→ routed `.ses` → KiCad → **gerbers**), *or* open the netlist+placement
  in **EasyEDA** (autoroute + one-click JLCPCB order). **We rent the
  autorouter; we pre-minimized its job** — and the §9 round-trip means a
  router failure bounces back to the placer, not to a human.
- **Mechanical → the 0041 bridge (immediate).** The **board outline +
  mounting/screw holes** export *now*, as a shared 2D profile a `cad` (0041)
  enclosure references directly — the cheap, high-value half of the bridge.
  A separate **component-block exporter** emits each placed part as a
  courtyard × package-height block (a coarse 3D height map) so a `cad`
  enclosure can check lid clearance / standoffs without the real 3D models.
  **Vias and screw-hole drills** are likewise their own export step. Each is
  a small exporter; a consumer takes the artifact(s) it needs.
- **Gate on binary presence at export only** (`kicad-cli`, the Freerouting
  jar). The *design loop has no external dependency* — authoring, probing,
  placement, BOM, DRC-lite, measures are all ours (0041 §12).
- Artifacts land on `PRECIS_CORPUS_DIR`; each exporter is a `job_type`
  returning a `CompileResult`-shape with `log_tail`, like the draft/`cad`
  export workers.

**v1 rents the router; phase 2 owns "the shove router"** (the user's "make
our own gerbers" + the §9 round-trip's logical conclusion). Renting first is
not a compromise — Freerouting/EasyEDA get a routed JLCPCB-ready board on day
one — but owning a coarse one is a *named phase-2 deliverable* (§13a), not a
maybe, because the conditions here make it unusually tractable.

#### 13a. The shove router (phase 2)

The standout property of *this* setting: the router has a **collaborator
that can move the obstacles.** A classic autorouter is hard almost entirely
because **placement is frozen** — it must thread copper through whatever
congestion the placement left, and the engineering (rip-up-and-retry,
conflict-driven search, topological rubber-banding, net-ordering heuristics)
all exists to claw out the **last few percent** of nets stuck in a tight
channel. That tail is the NP-hard-flavoured part. Our conditions **delete
the frozen-placement assumption**: when the router gets stuck it doesn't have
to be clever — it **escalates** ("I can't get these 6 nets through this
channel — shove that block 4 mm and rotate it"), the placer/LLM **opens
room** (never moving a `fixed` node, §4.1), and it re-routes. A global
completion problem becomes "route the easy 90% + a bounded local feedback
loop." That is the whole design:

- **Router core.** A **grid maze router (Lee / A\*)** — or a gridless
  topological "rubber-band" router — per net, vias = a layer change at a
  cost, on the 4-layer stack (H/V signal layers + GND/PWR planes keep the
  via story simple). Old, well-documented, visual-free algorithms. Routes
  the easy majority in a few hundred lines.
- **The escape hatch (§9 round-trip).** On a net it can't complete, **don't
  rip-up forever** — emit a **congestion report** (which nets, which region)
  the placer/LLM acts on, then re-route. `fixed` nodes are immovable
  obstacles; everything else is fair game to shove.
- **Intent as the cost function.** The **measures (§8.3)** *are* the router's
  weights — it already knows "keep `noisy` off `sensitive`," "respect the
  plane split," "this is a star net, route it as a star," "this is a return
  path, keep it short." A dumb router lacks exactly this; the LLM supplies
  it natively.
- **Own routing, rent verification + gerbers (the key hybrid).** Even when
  we own the router, **emit a KiCad PCB and let `kicad-cli` run the
  authoritative DRC and write the gerbers.** Our router can be merely
  "good enough"; a battle-tested tool catches anything it gets wrong and
  produces the fab files. This de-risks correctness almost entirely.

**Effort, honestly:** the maze core is *days*; rip-up + congestion reporting
~1–2 weeks; the JLCPCB DRC ruleset (min trace/space, annular ring, via/pad
geometry) ~1 week; planes/pours + thermal reliefs ~1 week+ (or defer). A
working coarse router for 4-layer, mostly-SMD, JLCPCB-rules boards is on the
order of **4–8 focused weeks** — not a multi-year Allegro clone — because we
only need to *complete a routable board*, not compete with commercial tools.
The honest hard edges (kept out of the phase-2 core): fine-pitch BGA escape
and very high density (mostly avoidable on JLCPCB proto boards — QFN/0402
dominate), controlled-impedance + diff-pair length matching, and pour DRC.

#### 13b. Why gerber generation is "rented," though it isn't *algorithmically* hard

Gerber output has **no search and no optimization** — so unlike routing it
isn't hard in the complexity sense. It's **rented because it is a wide
surface of finicky, format-level detail where a small mistake is silent and
expensive**:

- **Aperture model.** Gerber (RS-274X / Gerber X2) is aperture-based: define
  shapes (round / rect / obround / polygon / **aperture macros** for
  anything else), then *flash* or *draw* with them. Mapping every real pad
  shape to a correct aperture (and macro) is fiddly and easy to get subtly
  wrong.
- **Many coordinated files.** One gerber per copper / mask / paste / silk
  layer, **plus a separate Excellon drill file** (its own format), **plus**
  the X2/job attributes JLCPCB reads — all must agree on units, coordinate
  format (leading/trailing-zero suppression), and layer roles.
- **Region fill is the genuinely fiddly part.** Copper **pours/planes** are
  `G36/G37` polygon regions with correct winding, clearances, **thermal
  reliefs**, and knockouts — the same place an autorouter's pour logic is
  delicate.
- **Slow, expensive feedback.** A malformed aperture or region is **valid-
  looking but wrong**, and you only find out after paying for and receiving
  scrapped boards. Renting a battle-tested writer (kicad-cli) collapses that
  risk to zero.

So the rule of thumb stands: **own the parts that carry design *intent*
(selection, netlist, placement, measures, eventually routing); rent the
parts that are mature, format-heavy, and unforgiving (DRC + gerbers + fab).**

### 14. Mapping onto existing precis machinery

- **Kinds & codes.** `kind='pcb'` is a **refs-backed kind** (handle `pb`,
  one embedded card chunk, links, soft-delete) over the `pcb_*` graph tables.
  `kind='part'` is a **kind-handler over the `parts` catalog table**,
  addressed by its natural identity (the **LCSC C-number**, e.g.
  `get(kind='part', id='C25804')`), not chunks; a `pn` handle can be
  registered to resolve to the table if wanted. `corpus_role='none'` for
  both. `kind='datasheet'` (`da`) is a **thin `PaperHandler` subclass**
  (`corpus_role='evidence'`, `supports_put=False`, §7.1) — the electronics
  member of the paper family, sharing the ingest/reader/search engine
  wholesale. **In-tree, not a plugin** (handler + export workers + web viewer +
  the parts-refresh worker want to be in-tree, and the migration ships in
  the main chain), as 0041 decided for `cad`.
- **Storage (§4/§12, converging with 0041 Amdt 1).** The graph is **dedicated tables**,
  not chunks — corpus-membership isn't wanted for a netlist, and the data is
  a graph we query with joins + FK integrity. The *only* corpus artifact is
  the design's **one embedded `card_combined`** (LLM-authored one-line
  function summary + key parts + board size), so `search(kind='pcb', q=…)`
  finds the *board* by intent ("the I²C sensor node with the ESP32") without
  embedding a single component or net. Datasheets are `kind='datasheet'`
  documents (§7.1), searchable as themselves, scoped out of academic paper
  search.
- **Export worker.** A `job_type` (`workers/job_types/pcb_export.py`)
  shelling `kicad-cli`/Freerouting, recording artifact paths in
  `refs.meta`; the **`parts_refresh`** worker (system profile) keeps the
  catalog current.
- **Web.** Ratsnest **SVG** (airwires + crossings highlighted — the tracer
  lines) + the **BOM table** + probe/DRC results as the primary view; an
  optional human board viewer (vendored, like pdf.js). The agent never
  needs the pixels.
- **Agent loop.** `put` → re-evaluate → probe → edit on the `job` /
  `plan_tick` substrate; each design is a persisted, linked artifact
  (`derived-from` a datasheet, `produced-by` a todo, `has-requirement` a
  `cfp` for a spec'd board).
- **Skills.** `precis-pcb-help` (verbs + node model + the placement loop),
  `precis-part-select-help` (the selector verb + JLCPCB Basic/Extended/stock
  policy), `precis-net-class-help` (**the net-assignment skill the user
  named** — classify each net power/gnd/analog-sensitive/noisy-switching/
  high-speed/i2c/spi, from the datasheet *and* from general circuit
  reasoning; this drives trace-width DRC, plane assignment, and which
  measures apply), and `precis-measures-help` (the §8.3 measuring-tape
  library + how to tag component roles), plus the **domain skills the user
  asked for**: `precis-decoupling-help` (power bypass placement rules),
  `precis-i2c-help` (bus topology, pull-ups, terminator placement),
  `precis-spi-help` (bus fanout, length matching), and
  `precis-datasheet-help` ("how to read a datasheet" → pinout, abs-max,
  typical-app-circuit, decoupling). Index rows in `precis-overview` /
  `precis-help` / `precis-toolpath-help`.

### 15. A design is a sequenced project — orchestration (decided 2026-06-28)

**The convergence cycle (decided 2026-06-28):** finish the **netlist** →
**place** → **try routing** → if routing fails/congests, **fix the placement**
and **try routing again** → repeat until the board routes (or a bounded
attempt cap trips and the LLM is asked to intervene — split a net, change a
part, bump to more layers). The netlist is fixed during this loop; only the
placement moves. This *is* the §9 place↔route round-trip, and phases 5↔6 of
the machine below.

A board is built as **ordered phases**, each gated, on the **existing
`plan_tick` / job substrate** — *not* a free-running skill that self-drives
the whole flow. A `pcb` design is an `LLM:*` **project** (a `todo` carrying
`meta.workspace`, ADR 0027/CLAUDE.md); each phase is a child; the framework
owns the **state machine + gates**, the LLM (guided by a **per-phase skill**)
owns the **decisions within** a phase.

- **Why framework-driven, not a free-running skill.** Determinism, gating,
  resumability, and **short focused per-phase contexts** (the active-board
  context above keeps prompts terse). A free skill can skip a gate, lose
  state over a long session, and is hard to resume; the `plan_tick`
  coroutine + `auto_check` evaluators already give all of this.
- **Phases (with gates):**
  1. **Intent / requirements** (prompt or a `cfp` via `has-requirement`).
  2. **Architecture + datasheets** — pick main ICs, ingest/read datasheets.
  3. **Netlist + net-classes** — instantiate components, wire nets, add
     bypass/pull-up/terminator per datasheet, assign classes. *Gate:
     netlist DRC-lite clean (no unconnected pins, bypass present, …).*
  4. **Part selection** — concrete LCSC parts via the selector. *Gate: every
     component has an assemblable, in-stock `part`.*
  5. **Placement** — auto-place + measures + `fixed`. *Gate: courtyard-legal,
     hard measures satisfied.*
  6. **Route round-trip** — Freerouting headless; on failure, **back-edge to
     5** (§9). *Gate: route complete / DRC clean.*
  7. **Export / order** — BOM + CPL + gerbers, JLCPCB-orderable.
- **Not a one-way pipeline — explicit back-edges.** 6→5 is the §9 shove
  round-trip; "need a different part" is 5/4→4; "netlist was wrong" is
  any→3. The framework encodes the allowed transitions; a phase outcome can
  request a jump back.
- **Concurrency falls out for free.** Because phases are sequential, a
  netlist edit (phase 3) and a placement re-run (phase 5) are never
  concurrent — this is how open-Q "logical↔physical concurrency" is resolved,
  with no locking machinery beyond the row-level `FOR UPDATE` of §12.
- **Gates are `auto_check` evaluators** — new ones like `netlist_drc_clean`,
  `all_parts_selected`, `placement_legal`, `route_complete` join the existing
  `paper_ingested` / `child_job_succeeded` family.

## Phasing

> **v1 done-bar (decided 2026-06-28): an end-to-end *orderable* board.** v1
> ships when one known board (e.g. an ESP32-C3 sensor node) goes design →
> place → export → **JLCPCB-orderable** for real — so export + Freerouting
> (headless) are v1, not fast-follow.

- **v1**: the **relational graph** (`pcb_components` / `pcb_features` /
  `pcb_pins` / `pcb_instances` / `pcb_nets` / `pcb_netconns` / `pcb_measures` tables, §12 + role/class tags + the
  **`fixed` mark**, §4.1; design as a `refs` row with one card chunk), the
  **`part` catalog** table + JLCPCB-native selector (assemblable filter, Basic/stock/price
  ranking, pre-processed official feed), the **thin `datasheet` kind** (§7.1)
  + lazy ingest + `datasheet-of` link, the **eyes** (§8 probe ladder + logical signal trace + the measures
  library), **auto-kinda-place** (force-directed + annealing,
  crossings+length+soft-measures objective, `fixed` respected) and the
  **route-feasibility estimate**, the **place↔route round-trip** against the
  *rented* router, **BOM + CPL views**, the **mechanical exporters** (board
  outline + mounting/screw holes → the 0041 bridge; component-blocks),
  **export** to KiCad netlist / Specctra `.dsn` / JLCPCB BOM+CPL CSV,
  **renting Freerouting (headless)** for copper (EasyEDA = manual escape
  hatch only). 4-layer default, mm/float64/rigid-2.5D, the domain + net-class
  + measures skills.
- **Phase 2 — the shove router (§13a).** A precis-owned **coarse maze /
  rubber-band router** with the LLM-piloted **place↔route shove loop**
  (route easy 90% → congestion report → re-place the hot region, never moving
  `fixed` nodes → re-route), measures as its cost function, and the **own-
  routing-rent-gerbers hybrid** (emit KiCad, let `kicad-cli` DRC + write
  gerbers). Plus: **length-matching / differential-pair** measures with real
  routed-length math; richer **net current estimation**; richer **table
  extraction** for datasheets; the **full 0041 enclosure bridge** (3D
  component models, not just height blocks).

## Non-goals

- Routed-copper rendering / SPICE / signal-integrity simulation in the
  design loop (export / simulate elsewhere — 0041's "render elsewhere").
- A drawn **schematic with symbol graphics** — we keep the *netlist*, not a
  picture of it (pixels out).
- **Owning the autorouter** in v1 — *rent* **Freerouting headless** (the only
  one that automates; EasyEDA is GUI-only, a manual escape hatch). Owning a
  coarse one is a committed **phase-2** deliverable, §13a, not a "maybe."
- **Owning gerber generation / DRC**, ever — even when we own routing we
  rent these (§13b): mature, format-heavy, unforgiving, no design intent.
- Mechanical/3D — that's **0041**; 0042 emits a board outline that 0041 can
  *reference*, not a solid.
- Non-JLCPCB-assemblable parts in the default selector (a hard filter, by
  the user's requirement — overridable explicitly, never by default).

## Consequences

- **Good**: the LLM gets a *legible* circuit + board — netlist TOC,
  feature-attributed net/crossing probes, exact ratsnest length + crossing
  count, proximity + DRC-lite — at a fraction of a render's tokens; the
  netlist is correct **without a drawn schematic**; **manufacturability is
  by construction** (JLCPCB-assemblable + in-stock is the default, not a
  late surprise); **placement is pre-minimized**, so the rented autorouter
  starts from a near-planar layout (fewer vias, higher first-pass yield);
  **datasheets reuse paper ingest wholesale**; **dedicated tables** mean real
  SQL queries + FK integrity over the netlist and **no embedder load** on
  graph rows; the observer / job / web machinery is reused from 0041 and the
  draft stack; **zero GL in the design loop**.
- **Cost / risk**: we own a connectivity-and-placement evaluator, a **parts-
  catalog mirror** (the volatile part — stock/price drift, and the
  JLCPCB-assemblable flag must track JLCPCB's actual library), **and a small
  set of dedicated tables** (a migration + a handler that doesn't get the
  chunk reader for free — but we build a custom ratsnest view anyway, §14);
  routability is an *estimate* until the autorouter runs, so a placement the
  LLM believes routable can still fail (mitigated by the estimate being
  conservative and the rented router being authoritative); table extraction
  on datasheets is genuinely harder than on papers; renting two external
  tools (Freerouting/EasyEDA + JLCPCB) means the round-trip format (Specctra
  DSN / EasyEDA import) is a real integration surface.
- **Deferred**: own autorouter + gerbers, length-matching/diff-pairs,
  current estimation, richer datasheet tables, the 0041 enclosure bridge.

## Resolved (this discussion)

- **Parts feed** → **official feeds, heavily pre-processed before the LLM
  sees a row** (§5). (Exact endpoints + refresh cadence still TBD at
  implementation.)
- **How far to optimize placement** → **place light first; place *harder*
  only when the rented router actually fails** — the Xilinx-style
  place↔route round-trip (§9, §13a), with the LLM piloting the shove.
- **Net current / class** → **from the datasheet *and* from general circuit
  reasoning**, via the **`precis-net-class-help` skill** (§14); a net may
  also carry an explicit class/current override.
- **The 0041 bridge** → **yes, and partly immediate**: board outline +
  mounting holes export *now* as a shared profile; vias/screw-holes and
  component-blocks (height) are **separate exporters** a `cad` enclosure
  consumes; full 3D models are phase 2 (§13, §phasing).
- **Storage** → **dedicated relational tables, not chunks** (§4, §12): the
  netlist wants neither corpus-embedding nor a folded DAG; it wants SQL joins
  + FK integrity. The design stays a `refs` row with one embedded card so the
  *board* is still findable by intent.

### Decisions — 2026-06-28 (the four v1-gating questions)

- **v1 done-bar = an *end-to-end orderable board*.** v1 is not "done" until
  one known board (e.g. an ESP32-C3 sensor node) goes **design → place →
  export → JLCPCB-orderable** for real. This forces the whole pipeline early;
  it makes export + the rented router v1 work, not fast-follow.
- **Parts / footprint / pin data → `easyeda2kicad` + the JLCPCB parts CSV.**
  No paid LCSC API assumed. The open **`easyeda2kicad`** tool supplies the
  per-part **footprint geometry + pin-name→pad map + 3D** (pulled from LCSC);
  the downloadable **JLCPCB assembly parts list** supplies the
  `jlcpcb_assemblable` flag + Basic/Extended + stock. LCSC parametrics/stock
  layer on top. (This resolves the previously-vague "from the footprint /
  datasheet pinout" — footprints/pins are a *data pipeline*, the catalog's
  hardest part. Octopart/Nexar stays an optional parametrics backfill.)
- **v1 router = Freerouting *headless*.** Only Freerouting has a batch /
  headless mode (`.dsn`→`.ses`), so it is the one router the §9 place↔route
  round-trip can drive automatically. **EasyEDA is GUI-only** → it stays a
  *manual* human escape hatch, never in the automated loop.
- **Authoring = records traversed as a graph, *not* a netlist blob/DSL.** The
  netlist lives as `pcb_*` rows; the LLM **navigates it as a graph** — `get`
  IC4 → its pins → the net on each pin → reason → walk to the connected
  components — and edits by `put`/`edit` on individual records. So the
  first-class surface is **graph traversal** (§4 addressing, §8.2 signal
  trace), not an import format. (No DSL or KiCad/SPICE import in v1; either
  could be added later as sugar over the same rows.) This makes §8.2's
  walk — *and a `get(neighbors)`-style hop from any pin/net/component* — the
  load-bearing read path, elevated from "one probe among many."

### Decisions — 2026-06-28 (round 2)

- **Stock liveness** *(amended in round 3 — see below; was "live at selection",
  which contradicts the no-API daily dump).* **Price** folds into the rank
  alongside `basic`/`stock`.
- **Measure expressiveness = the fixed library, no mini-DSL.** The datasheet
  gives the LLM enough to make an "acceptable" call; its judgment fills any
  gap. (Resolves open-Q 4.)
- **Soft-measure weights = per-measure-type defaults + LLM override.** Ship
  sensible default weights (a sensitive↔noisy `separation` outweighs a
  cosmetic gap); the LLM may override per measure when it has a reason, but
  never *must* think about weights. The §9 round-trip *is* the place↔route
  hand-off, so there is no separate threshold to tune. (Resolves open-Q 2.)
- **Active-board context ("`use pcb12`").** Addressing is `pb12#U3` = handle
  code `pb` · ref 12 · component `U3`. To keep phase prompts short, a session
  may **select an active design** (`get(kind='pcb', id='pb12',
  view='use')` / a session param), after which **bare** `U3` / `SCL` /
  `U3.SCL` resolve against it; the fully-qualified `pb12#U3` always works for
  disambiguation or cross-references (a `gripe` pointing at one component).
  So **`pb12#U3` *is* the global handle** (path form) — no separate per-row
  integer handle. (Resolves open-Q 6 on sub-row handles.)
- **A design is a *sequenced project*; phases solve concurrency.** The build
  runs as ordered **phases** (§15), so a netlist edit and a placement re-run
  are never concurrent — concurrency is handled *by phasing*, no extra
  machinery. (Resolves open-Q 3.) Orchestration is the existing
  `plan_tick`/job substrate (§15), **not** a free-running skill.

### Decisions — 2026-06-28 (round 3; full detail in the schema proposal)

- **Schema** locked — full DDL in
  [`docs/design/pcb-schema-proposal.md`](../design/pcb-schema-proposal.md).
  Key calls: **nothing FKs the catalog** (the atomic-swap `DROP parts` forbids
  it; designs loose-point `parts.lcsc` and **snapshot** footprint/courtyard/
  height/value so a board survives catalog churn); `pcb_netconns UNIQUE(instance,
  pin)` = *a pin is on at most one net*; **child soft-delete = `retired_at`**
  (house convention), `pcb_netconns` hard-delete; class/metric/ftype = free text +
  handler-validated enum.
- **Stock → turnover, not live count (amends round 2).** The goal is to avoid
  the *last-reel-ever* part and prefer **high-turnover** parts — a velocity
  question no live count answers. Keep the daily dump, **derive a per-part
  turnover/availability score** by diffing successive dumps (restock events,
  trend, EWMA) into a separate **`part_availability`** table (survives the
  swap), and **rank on that** (Basic + turnover + healthy trend). No live
  scrape; the JLCPCB order is the final buyable-now gate. v1 may start with a
  proxy (Basic + stock-threshold + age).
- **Write path: batch `put`** (one call takes *lists* of components/nets/conns
  — same verb, array args, *not* a netlist DSL/format). Graph-traversal is the
  *read* model; batch put the *write* model. Never one `put` per row.
- **Handle codes:** `pcb`→`pb`, `part`→`pn` (other-table, C-number-addressed),
  `datasheet`→`da` (chunk `dk`). LCSC C-number is the stable rejoin key
  (`mfr_part` the deeper identity).
- **Discoverability: EDA kinds are context-gated** — usable but **not** in the
  always-loaded kind catalog (would tax every non-EDA session). A conditional
  module (ADR 0038) + an EDA skill group (ADR 0032), discovered via
  `search(kind='skill', …)`. Generalizes to niche kinds.
- **Footprint pipeline = tiered (internalize progressively).** v1 **rents
  `easyeda2kicad`** (pad geometry + pinout in one fetch). Phase 2 internalizes
  the standard case: an **IPC-7351 land-pattern generator** for standard
  packages (rules from package+dims — offline, ~80–90%, kills the SPOF) +
  **datasheet pinout extraction** for the name→pad map; fetch-fallback for the
  proprietary tail. (Routing-internalization is already the phase-2 shove
  router, §13a.)
- *Confirmed:* v1 = **walking skeleton** (trivial board end-to-end first);
  layer default **4** (no threshold).

### Decisions — 2026-06-28 (round 4; finalize schema — full DDL in the proposal)

- **Type/instance split** — `pcb_components` is the *type* (owns `pcb_pins`);
  `pcb_instances` are placements (refdes/x/y/rot/layer/fixed/roles); the
  netlist is `pcb_netconns(net, instance, pin)`; **a physical pin is on ≤1
  net**; **composite FKs** force a netconn's pin+instance to share a
  component. Repeated parts (passives) define the type once. A **put
  convenience** creates component+instance together for the unique-part case.
- **Notes everywhere** — `note text` on components/pins/instances/nets/
  netconns/features (measures use `reason`): store the LLM's reasoning (the
  *why* of a wire/placement). Inline, not embedded.
- **Pins carry `tags text[]`** (electrical type + domain: input/output/bidir/
  passive/power/gnd/5v/data/analog…), not a flat enum; GIN-indexed; decoupling
  DRC keys on `power ∈ tags` and degrades gracefully if sparse.
- **Net name required + meaningful** (the net's documented purpose; no `N$1`).
- **Coordinate/rotation** — see §3 (centroid pivot, CW-from-north, board-corner
  origin, exporters convert, JLCPCB CPL fix map).
- **Trace width** — `pcb_nets.width_mm` derived from class default ∨ IPC-2221
  current calc, **snapped to standard steps**; copper weight + width-table on
  `refs.meta`. Wires/vias are the router's output (not stored in v1).
- **Plane nets** modeled fully in `pcb_netconns`; excluded only from the
  derived ratsnest/crossing metric (a Slice-4 rule, not a schema flag).
- **Best-so-far placement** snapshot in `refs.meta` for the convergence loop.

## Open questions (remaining)

1. **Router cost-function tuning** (phase 2, when we own a router, §13a) — how
   to weight the measures (§8.3) against shortest-path + via count so the
   LLM's intent steers the route without making it un-routable.

*(All v1-shaping questions resolved 2026-06-28 — see the Decisions blocks
above. Only the phase-2 router-tuning question remains.)*

### Lower-stakes defaults (chosen unless you object)

- **Catalog scope** → mirror **only JLCPCB-assemblable** parts (~tens of
  thousands), not all of LCSC.
- **DRC-lite rules** → **hardcode JLCPCB's published capability matrix**
  (min trace/space/via per layer count), versioned in one table.
- **Placer / router execution** → a **`job`** (async), like exports, since
  annealing + the external router round-trip take seconds-to-minutes.

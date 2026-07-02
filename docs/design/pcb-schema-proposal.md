# PCB (ADR 0042) — DB schema proposal (v2)

> Slice 1's core. Reviewable proposal for the `0042_pcb_kind.sql` migration.
> Grounded in the `0041_cad_kind.sql` precedent + the `refs`/`chunks`/`kinds`
> core. Forward-only (ADR 0005); regenerate the baseline after merge (0031).
> **Status: proposed — final read before sealing.** v2 = the type/instance
> split, first-class pins + netconns, notes everywhere, trace width.

## Conventions followed (from the codebase)

- **Kinds register in the `kinds` table** + get a handle code.
- **Child-row soft-delete = `retired_at`** (the `cad_nodes` precedent), not
  `deleted_at`. The design *ref* uses `refs.deleted_at` (unchanged).
- **PKs = `bigint GENERATED ALWAYS AS IDENTITY`**; `REFERENCES refs(ref_id)
  ON DELETE CASCADE`.
- Idempotent, `BEGIN/COMMIT`, `COMMENT ON TABLE`, partial indexes
  `WHERE retired_at IS NULL`.

## The model — type / instance / pin / net / netconn

The normalized schematic model (per the design discussion):

- **component** = a part *type* used in this design (owns its **pins**);
  design-local, loose-references a catalog SKU, snapshots footprint data.
- **instance** = a *placement* (`U3`) of a component — coordinates, layer,
  fixed, roles.
- **pin** = a pin of the component *type* (pad + function name + tags).
- **net** = a named, classed signal.
- **netconn** = the connection triple **(net, instance, pin)**; a net has
  many. A *physical* pin = (instance, pin) is on **at most one net**.

Repeated parts fall out cleanly: 50 identical caps = **one** component (2
pins) + 50 instances; the BOM is "count instances per component."

## Two invariants this adds (coordinate frame + rotation)

- **Frame:** millimetres; **origin (0,0) at the board-outline reference
  corner**, **+X right, +Y up (north)**. All instance/feature coordinates are
  in this one frame.
- **Placement reference + rotation pivot = the footprint *centroid*** (the
  pick-&-place pickup point — what CPL needs, rotation-stable). Snapshotted
  onto the component.
- **Rotation = degrees clockwise from north (+Y), positive** — our *internal*
  convention. **Exporters convert** to each target (KiCad/gerber are
  CCW-from-+X; **JLCPCB CPL rotation is a known footgun → a per-package
  rotation-fix map** in the CPL exporter).
- **Bottom-layer instances mirror in X.**

## The load-bearing call: nothing FKs the catalog

The catalog (`parts`) is refreshed by **atomic swap** (`DROP parts`), so a FK
*into* it would break the swap — and a discontinued part must not
cascade-break a saved design. So design rows **loose-point** `part_lcsc`
(text) and **snapshot** footprint/courtyard/centroid/height/value. Catalog
(`parts`/`part_footprints`/`part_availability`) and design graph (`pcb_*`) are
**FK-disjoint islands**, joined only by the loose C-number.

## DDL (proposed `0042_pcb_kind.sql`)

```sql
BEGIN;

-- 1. kinds -----------------------------------------------------------
INSERT INTO kinds (slug, is_numeric, title, description) VALUES
  ('pcb', FALSE, 'PCB',
   'Electronics/PCB design (ADR 0042) — a netlist + placement graph in '
   'dedicated tables, read+authored by the LLM as a traversable graph '
   '(ratsnest/measures/signal-trace), never pixels. JLCPCB-native. '
   'Postgres-canonical; Freerouting/gerbers/fab are downstream export. '
   'See precis-pcb-help.'),
  ('part', FALSE, 'Part',
   'LCSC/JLCPCB catalog part (ADR 0042 §5) — reference data in the `parts` '
   'table, addressed by LCSC C-number. Ingest-only (jlcparts dump); not '
   'embedded. See precis-part-select-help.'),
  ('datasheet', FALSE, 'Datasheet',
   'Component datasheet (ADR 0042 §7) — a thin PaperHandler sibling '
   '(corpus_role=evidence) via the Marker→chunks pipeline, linked '
   'datasheet-of a part. One kind for the electronics-doc family. '
   'See precis-datasheet-help.')
ON CONFLICT (slug) DO NOTHING;

-- 2. pcb_components — the component TYPE (owns pins) ------------------
CREATE TABLE IF NOT EXISTS pcb_components (
  component_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ref_id     bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
  label      text   NOT NULL,                  -- "ESP32-C3" / "100nF 0402"
  part_lcsc  text,                             -- chosen catalog SKU (LOOSE, no FK); NULL pre-selection
  footprint  text,                             -- snapshot
  courtyard  jsonb,                            -- snapshot {w,h} or polygon (mm)
  centroid   jsonb,                            -- snapshot {x,y}: pick-place point = rotation/placement pivot
  height_mm  double precision,                 -- snapshot (height measure §8.3)
  note       text,                             -- LLM reasoning (why this part/type)
  meta       jsonb  NOT NULL DEFAULT '{}',
  retired_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (component_id, ref_id)                -- design scoping (+ allows composite refs)
);
CREATE INDEX IF NOT EXISTS pcb_components_ref_idx
  ON pcb_components (ref_id) WHERE retired_at IS NULL;

-- 3. pcb_pins — pins of a component TYPE -----------------------------
CREATE TABLE IF NOT EXISTS pcb_pins (
  pin_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  component_id bigint NOT NULL REFERENCES pcb_components (component_id) ON DELETE CASCADE,
  pad          text,                           -- "7"/"A1" physical pad (NULL until bound)
  name         text   NOT NULL,                -- "SCL"/"VDD" function name
  tags         text[] NOT NULL DEFAULT '{}',   -- electrical type + domain/role: input|output|bidir|passive|power|nc|analog|clock|data|gnd|5v|3v3…
  description  text,                           -- datasheet function/notes
  note         text,                           -- LLM reasoning
  meta         jsonb  NOT NULL DEFAULT '{}',
  retired_at   timestamptz,
  UNIQUE (pin_id, component_id)                -- for the composite integrity FK
);
CREATE UNIQUE INDEX IF NOT EXISTS pcb_pins_comp_name_key
  ON pcb_pins (component_id, name) WHERE retired_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS pcb_pins_comp_pad_key
  ON pcb_pins (component_id, pad)  WHERE retired_at IS NULL AND pad IS NOT NULL;
CREATE INDEX IF NOT EXISTS pcb_pins_comp_idx ON pcb_pins (component_id) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS pcb_pins_tags_gin  ON pcb_pins USING gin (tags);

-- 4. pcb_instances — a placement (U3) --------------------------------
CREATE TABLE IF NOT EXISTS pcb_instances (
  instance_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ref_id       bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
  component_id bigint NOT NULL REFERENCES pcb_components (component_id) ON DELETE CASCADE,
  refdes       text   NOT NULL,                -- "U3" (unique per design)
  x            double precision,               -- centroid location (mm, board-corner origin); NULL until placed
  y            double precision,
  rot          double precision NOT NULL DEFAULT 0,   -- deg CW from north (+Y); export converts
  layer        text   NOT NULL DEFAULT 'top',  -- top | bottom (bottom mirrors X)
  fixed        text,                           -- NULL | 'xy' | 'rot' | 'both' (§4.1)
  roles        text[] NOT NULL DEFAULT '{}',   -- sensitive|noisy|hot|temp-sensitive…
  note         text,                           -- LLM reasoning (why here / why this role)
  meta         jsonb  NOT NULL DEFAULT '{}',
  retired_at   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (instance_id, component_id),          -- for the composite integrity FK
  CONSTRAINT pcb_instances_layer_chk CHECK (layer = ANY (ARRAY['top','bottom'])),
  CONSTRAINT pcb_instances_fixed_chk CHECK (fixed IS NULL OR fixed = ANY (ARRAY['xy','rot','both']))
);
CREATE UNIQUE INDEX IF NOT EXISTS pcb_instances_ref_refdes_key
  ON pcb_instances (ref_id, refdes) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS pcb_instances_ref_idx       ON pcb_instances (ref_id) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS pcb_instances_component_idx ON pcb_instances (component_id);
CREATE INDEX IF NOT EXISTS pcb_instances_roles_gin     ON pcb_instances USING gin (roles);

-- 5. pcb_nets -------------------------------------------------------
CREATE TABLE IF NOT EXISTS pcb_nets (
  net_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ref_id     bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
  name       text   NOT NULL,                  -- REQUIRED + meaningful (SCL, VCC3V3, MOTOR_PWM) — the net's "meaning"
  net_class  text,                             -- signal|power|gnd|analog|i2c|spi|high_speed…
  est_current_a double precision,              -- worst-case current → width
  width_mm   double precision,                 -- assigned trace width; NULL = derive (class default ∨ IPC calc), snapped to a step
  note       text,                             -- LLM reasoning
  meta       jsonb  NOT NULL DEFAULT '{}',     -- layer hint, width override…
  retired_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS pcb_nets_ref_name_key
  ON pcb_nets (ref_id, name) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS pcb_nets_ref_idx ON pcb_nets (ref_id) WHERE retired_at IS NULL;

-- 6. pcb_netconns — (net, instance, pin); the netlist ----------------
CREATE TABLE IF NOT EXISTS pcb_netconns (
  netconn_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  net_id       bigint NOT NULL REFERENCES pcb_nets (net_id) ON DELETE CASCADE,
  instance_id  bigint NOT NULL,
  pin_id       bigint NOT NULL,
  component_id bigint NOT NULL,                -- denormalized so the two FKs below force pin.component = instance.component
  note         text,                           -- LLM reasoning about THIS connection (why this wire)
  meta         jsonb  NOT NULL DEFAULT '{}',
  created_at   timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (instance_id, component_id)
    REFERENCES pcb_instances (instance_id, component_id) ON DELETE CASCADE,
  FOREIGN KEY (pin_id, component_id)
    REFERENCES pcb_pins (pin_id, component_id)           ON DELETE CASCADE
);
-- a physical pin (instance,pin) is on AT MOST ONE net
CREATE UNIQUE INDEX IF NOT EXISTS pcb_netconns_phys_pin_key
  ON pcb_netconns (instance_id, pin_id);
CREATE INDEX IF NOT EXISTS pcb_netconns_net_idx      ON pcb_netconns (net_id);
CREATE INDEX IF NOT EXISTS pcb_netconns_instance_idx ON pcb_netconns (instance_id);

-- 7. pcb_measures — the measuring tapes (§8.3) ----------------------
CREATE TABLE IF NOT EXISTS pcb_measures (
  measure_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ref_id     bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
  metric     text   NOT NULL,                  -- separation|proximity|parallelism|supply_path|topology|plane_continuity|height|thermal
  direction  text,                             -- min|max|target|keep_above|keep_below
  goal       double precision,
  strength   text   NOT NULL DEFAULT 'gauge',  -- hard | soft | gauge
  weight     double precision,                 -- soft term weight; NULL = per-metric default
  operands   jsonb  NOT NULL,                  -- refs to INSTANCES/nets/classes: [{instance:id}|{net:id}|{role:tag}|{netclass:tag}|{pin:[inst,pin]}]
  reason     text,                             -- the intent ("I2C terminator")
  meta       jsonb  NOT NULL DEFAULT '{}',
  retired_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pcb_measures_strength_chk CHECK (strength = ANY (ARRAY['hard','soft','gauge']))
);
CREATE INDEX IF NOT EXISTS pcb_measures_ref_idx ON pcb_measures (ref_id) WHERE retired_at IS NULL;

-- 8. pcb_features — non-electrical placed (mounting holes, outline…) -
CREATE TABLE IF NOT EXISTS pcb_features (
  feature_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ref_id     bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
  ftype      text   NOT NULL,                  -- mounting_hole|fiducial|testpoint|keepout|outline
  x          double precision,
  y          double precision,
  rot        double precision NOT NULL DEFAULT 0,
  layer      text,                             -- top|bottom|all (NULL = through/all)
  fixed      text,
  geom       jsonb,                            -- hole Ø, keepout poly, outline path (mm)
  note       text,
  meta       jsonb  NOT NULL DEFAULT '{}',
  retired_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pcb_features_ref_idx ON pcb_features (ref_id) WHERE retired_at IS NULL;

-- 9. parts — Flow A bulk catalog (swapped wholesale; NO inbound FK) --
CREATE TABLE IF NOT EXISTS parts (
  lcsc       text PRIMARY KEY,                 -- "C25804"
  mfr        text,
  mfr_part   text,
  description text,
  jlcpcb_assemblable boolean NOT NULL DEFAULT false,
  basic      boolean NOT NULL DEFAULT false,
  stock      integer,
  price      jsonb,
  package    text,
  height_mm  double precision,
  params     jsonb,
  datasheet_url text,
  description_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', coalesce(description,''))) STORED,
  refreshed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS parts_select_idx
  ON parts (jlcpcb_assemblable, basic, stock DESC) WHERE jlcpcb_assemblable;
CREATE INDEX IF NOT EXISTS parts_tsv_gin    ON parts USING gin (description_tsv);
CREATE INDEX IF NOT EXISTS parts_params_gin ON parts USING gin (params);

-- 10. part_footprints — Flow B lazy easyeda2kicad cache --------------
CREATE TABLE IF NOT EXISTS part_footprints (
  lcsc       text PRIMARY KEY,                 -- loose ref to parts.lcsc (no FK)
  pads       jsonb,
  pin_map    jsonb,                            -- pad → name + tags (materializes pcb_pins)
  courtyard  jsonb,
  centroid   jsonb,                            -- pick-place point
  kicad_mod  text,
  model_3d   text,
  source     text,                             -- easyeda2kicad version
  fetched_at timestamptz NOT NULL DEFAULT now()
);

-- 11. part_availability — turnover signal (survives the swap) --------
CREATE TABLE IF NOT EXISTS part_availability (
  lcsc          text PRIMARY KEY,
  stock_now     integer,
  stock_prev    integer,
  ewma_stock    double precision,
  restock_count integer NOT NULL DEFAULT 0,
  last_restock_at timestamptz,
  trend         double precision,              -- <0 only-draining = last-reel risk
  first_seen    timestamptz NOT NULL DEFAULT now(),
  discontinued  boolean NOT NULL DEFAULT false,
  updated_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE pcb_components    IS 'PCB component TYPE (ADR 0042 §4) — owns pins; loose-refs a catalog SKU; snapshots footprint/centroid so a design survives catalog churn.';
COMMENT ON TABLE pcb_pins          IS 'Pins of a component type — pad + function name + tags (electrical type/domain). note = LLM reasoning.';
COMMENT ON TABLE pcb_instances     IS 'A placement (refdes) of a component — centroid x/y, rot (CW from north), layer, fixed, roles, note.';
COMMENT ON TABLE pcb_nets          IS 'PCB nets — REQUIRED meaningful name (the net''s purpose), class, est current, derived width.';
COMMENT ON TABLE pcb_netconns      IS 'The netlist: (net, instance, pin). A physical pin is on at most one net. Composite FKs force pin.component = instance.component. note = why this wire.';
COMMENT ON TABLE pcb_measures      IS 'Measuring tapes (§8.3) — hard/soft/gauge intent over INSTANCES/nets/classes; re-evaluated on change.';
COMMENT ON TABLE pcb_features      IS 'Non-electrical placed features (§4): mounting holes, fiducials, keepouts, board outline.';
COMMENT ON TABLE parts             IS 'LCSC/JLCPCB catalog (§5, Flow A) — bulk from jlcparts via staging+atomic-swap. NO inbound FK.';
COMMENT ON TABLE part_footprints   IS 'easyeda2kicad footprint cache (§5, Flow B) — lazy per selected part; survives the swap.';
COMMENT ON TABLE part_availability IS 'Per-part turnover signal (§5) — diffed from daily dumps; selection ranks on this, not live stock.';

COMMIT;
```

## Rationale / notable choices

- **`note text` on every authored row** (components, pins, instances, nets,
  netconns, features; measures use `reason`). Stores the LLM's reasoning — the
  *why* of a connection / placement is as valuable as the fact. Inline, not
  embedded (the graph stays out of the corpus); searchable-reasoning would be
  a phase-2 embed.
- **Pin electrical info is `tags text[]`**, not a flat enum — direction
  (`input/output/bidir/passive`) + domain/role (`power/5v/gnd/data/analog`)
  are different axes; a pin is `{power,5v}` or `{bidir,i2c}`. Controlled vocab,
  handler-validated, GIN-indexed. Decoupling DRC keys on `power` ∈ tags (and
  **degrades gracefully** if tags are sparse — `easyeda2kicad` may not give
  electrical type; datasheet/LLM enrich).
- **Net name required + meaningful** — the name *is* the net's documented
  purpose (a note for the LLM later); no silent `N$1`.
- **`pcb_netconns` hard-delete** (no `retired_at`); re-wire = delete+insert;
  recoverability is at instance/net level. The **composite FKs**
  (`(instance_id,component_id)` + `(pin_id,component_id)`) structurally forbid
  wiring a pin to an instance of a different component — no trigger needed.
- **Plane nets (gnd/power) are modeled fully** in `pcb_netconns` (every GND
  pin is a real row). They are **excluded only from the derived ratsnest /
  crossing-metric** (they drop to the plane; the router handles them) — a
  Slice-4 derivation rule, *not* a schema flag.
- **Best-so-far placement** kept by the convergence loop as a JSON snapshot in
  `refs.meta` (revert if a re-place worsens routing). v2-table only if needed.
- **The board:** stackup + per-layer **copper weight** + **net-class width
  table** + layer-count live on `refs.meta`; the outline polygon + mounting
  holes are `pcb_features`. The derived layer (ratsnest/crossings/measure
  verdicts) is computed-on-read, memoized per `(ref_id, rev)` (rev in
  `refs.meta`, bumped on any graph write).
- **One card chunk** (`chunk_kind='card_combined'`, `ord<0`) is the only
  corpus artifact — what `search(kind='pcb')` embeds.

## Handles (`utils/handle_registry.py`)

```python
KIND_CODES        += { 'pcb': 'pb', 'part': 'pn', 'datasheet': 'da' }
CHUNK_CODES       += { 'datasheet': 'dk' }
_OTHER_TABLE_KINDS |= { 'part' }   # addressed by C-number, not a decimal handle
```
- `pb` (pcb) / `da` (datasheet) refs-backed → full resolution. `pn` (part)
  reserved; addressed by **LCSC C-number**.
- Sub-rows by **design-scoped path**: `pb12#U3` (instance), `pb12#U3.SCL`
  (its pin/netconn), `pb12@SCL` (net); active-board `use pcb12` allows bare
  forms. `pb12#U3` *is* the global handle (no per-row integer).

## Discoverability — EDA kinds are context-gated (ADR 0038)

`pcb`/`part`/`datasheet` are usable but **not** in the always-loaded kind
catalog (would tax every non-EDA session). A conditional module + an EDA
skill group (ADR 0032), discovered via `search(kind='skill', …)`.

## Footprint strategy — tiered (E)

- **v1:** rent `easyeda2kicad` (pad geometry + pinout in one fetch).
- **Phase 2:** internal **IPC-7351 land-pattern generator** for standard
  packages (rules from package+dims; ~80–90%, kills the SPOF) + **datasheet
  pinout extraction** for the name/tag map; fetch-fallback for the tail.

## Routing-storage boundary

- **v1:** routed traces + vias are NOT stored — the rented router's output is
  the `.ses`/gerbers artifact (path in `refs.meta`). Only the *estimate*
  (ratsnest/crossings/via-count) is computed-on-read.
- **Phase 2 (own router):** `pcb_traces` + `pcb_vias` (full geometry, for
  export + real routed-length measures); LLM still reads digests, not vertex
  lists. Mounting/screw holes are `pcb_features` *now*.

## Resolved (2026-06-28)

Schema micro-decisions: free-text + handler-validated enum for
class/metric/ftype; `pcb_netconns` hard-delete + composite-FK integrity;
`stock integer`; `part` in `kinds` as other-table; `da`/`dk` codes;
notes-everywhere; pin `tags[]`; required net names; centroid pivot; CW-from-
north rotation; stock→turnover; batch `put`; footprint tiers; routing
boundary; convergence cycle (ADR §15).

## Still open (don't block sealing)

- `parts_refresh` cadence (daily matches `jlcparts`).
- Phase-2 router cost-function tuning.

-- 0047_pcb_kind.sql
--
-- ADR 0042 — the `pcb` kind: a netlist + placement IR the LLM reads and
-- authors as a *traversable graph* (ratsnest / measures / signal-trace),
-- never pixels. JLCPCB-native. Postgres is canonical; Freerouting (routing)
-- and gerbers/BOM/CPL (fab) are downstream export.
--
-- Storage (ADR 0042 §4/§12, converging with 0041 Amendment 1): the design is
-- a `ref` (kind='pcb', links, tags) keeping ONE `card_combined` chunk for
-- intent-search; the graph lives in the dedicated `pcb_*` tables below — a
-- normalized type/instance model:
--   * pcb_components  — a component TYPE used in this design; owns its pins.
--   * pcb_pins        — pins of a type (pad + function name + electrical tags).
--   * pcb_instances   — a placement (refdes U3) of a component.
--   * pcb_nets        — a named, classed signal (name REQUIRED + meaningful).
--   * pcb_netconns    — the netlist triple (net, instance, pin); a physical
--                       pin is on <=1 net; composite FKs force the pin and the
--                       instance to share a component.
--   * pcb_measures    — the "measuring tapes" (hard/soft/gauge design intent).
--   * pcb_features    — non-electrical placed geometry (mounting holes, board
--                       outline, fiducials, keepouts).
-- and the LCSC/JLCPCB catalog (FK-disjoint from the design graph so the daily
-- atomic swap can DROP it):
--   * parts            — Flow A bulk catalog (jlcparts dump, staging+swap).
--   * part_footprints  — Flow B lazy easyeda2kicad cache (survives the swap).
--   * part_availability— per-part turnover signal (selection ranks on this).
--
-- Conventions: child-row soft-delete is `retired_at` (the cad_nodes / ADR
-- 0033 precedent); `pcb_netconns` is hard-delete (edges are membership).
-- `note` on every authored row stores the LLM's reasoning (the why-of-a-wire).
-- Coordinate frame: mm, origin (0,0) at the board-outline corner, +X right /
-- +Y up; an instance (x,y) is its footprint centroid (pick-place point), `rot`
-- is degrees clockwise from north (+Y) — exporters convert.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot after
-- merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

-- 1. the ref kinds ---------------------------------------------------
INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('pcb', FALSE, 'PCB',
     'Electronics/PCB design (ADR 0042) — a netlist + placement graph in '
     'dedicated tables, read and authored by the LLM as a traversable graph '
     '(ratsnest / measures / signal-trace), never pixels. JLCPCB-native. '
     'Postgres-canonical; Freerouting/gerbers/fab are downstream export. '
     'See precis-pcb-help.'),
    ('part', FALSE, 'Part',
     'LCSC/JLCPCB catalog part (ADR 0042) — reference data in the `parts` '
     'table, addressed by LCSC C-number. Ingest-only (jlcparts dump); not '
     'embedded. See precis-part-select-help.'),
    ('datasheet', FALSE, 'Datasheet',
     'Component datasheet (ADR 0042) — a thin PaperHandler sibling '
     '(corpus_role=evidence) ingested via the Marker->chunks pipeline and '
     'linked datasheet-of a part. One kind for the whole electronics-doc '
     'family (app-note/errata via a meta sub-type). See precis-datasheet-help.')
ON CONFLICT (slug) DO NOTHING;

-- 2. pcb_components — the component TYPE (owns pins) -----------------
CREATE TABLE IF NOT EXISTS pcb_components (
    component_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ref_id     bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    label      text   NOT NULL,                    -- "ESP32-C3" / "100nF 0402"
    part_lcsc  text,                               -- chosen catalog SKU (LOOSE, no FK)
    footprint  text,                               -- snapshot
    courtyard  jsonb,                              -- snapshot {w,h} or polygon (mm)
    centroid   jsonb,                              -- snapshot {x,y}: pick-place / rotation pivot
    height_mm  double precision,                   -- snapshot
    note       text,                               -- LLM reasoning (why this part/type)
    meta       jsonb  NOT NULL DEFAULT '{}',
    retired_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (component_id, ref_id)
);

COMMENT ON TABLE pcb_components IS
    'PCB component TYPE (ADR 0042 §4) — owns pcb_pins; loose-refs a catalog '
    'SKU; snapshots footprint/centroid so a design survives catalog churn.';

CREATE INDEX IF NOT EXISTS pcb_components_ref_idx
    ON pcb_components (ref_id) WHERE retired_at IS NULL;

-- 3. pcb_pins — pins of a component TYPE ----------------------------
CREATE TABLE IF NOT EXISTS pcb_pins (
    pin_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    component_id bigint NOT NULL REFERENCES pcb_components (component_id) ON DELETE CASCADE,
    pad          text,                             -- "7"/"A1" physical pad (NULL until bound)
    name         text   NOT NULL,                  -- "SCL"/"VDD" function name
    tags         text[] NOT NULL DEFAULT '{}',     -- input|output|bidir|passive|power|nc|analog|clock|data|gnd|5v|3v3...
    description  text,                             -- datasheet function/notes
    note         text,                             -- LLM reasoning
    meta         jsonb  NOT NULL DEFAULT '{}',
    retired_at   timestamptz,
    UNIQUE (pin_id, component_id)                  -- for the composite integrity FK
);

COMMENT ON TABLE pcb_pins IS
    'Pins of a component type (ADR 0042) — pad + function name + electrical '
    'tags. note = LLM reasoning.';

CREATE UNIQUE INDEX IF NOT EXISTS pcb_pins_comp_name_key
    ON pcb_pins (component_id, name) WHERE retired_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS pcb_pins_comp_pad_key
    ON pcb_pins (component_id, pad) WHERE retired_at IS NULL AND pad IS NOT NULL;
CREATE INDEX IF NOT EXISTS pcb_pins_comp_idx
    ON pcb_pins (component_id) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS pcb_pins_tags_gin
    ON pcb_pins USING gin (tags);

-- 4. pcb_instances — a placement (U3) ------------------------------
CREATE TABLE IF NOT EXISTS pcb_instances (
    instance_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ref_id       bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    component_id bigint NOT NULL REFERENCES pcb_components (component_id) ON DELETE CASCADE,
    refdes       text   NOT NULL,                  -- "U3" (unique per design)
    x            double precision,                 -- centroid location (mm); NULL until placed
    y            double precision,
    rot          double precision NOT NULL DEFAULT 0,  -- deg CW from north (+Y); export converts
    layer        text   NOT NULL DEFAULT 'top',    -- top | bottom (bottom mirrors X)
    fixed        text,                             -- NULL | 'xy' | 'rot' | 'both'
    roles        text[] NOT NULL DEFAULT '{}',     -- sensitive|noisy|hot|temp-sensitive...
    note         text,                             -- LLM reasoning (why here / why this role)
    meta         jsonb  NOT NULL DEFAULT '{}',
    retired_at   timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (instance_id, component_id),            -- for the composite integrity FK
    CONSTRAINT pcb_instances_layer_chk CHECK (layer = ANY (ARRAY['top'::text, 'bottom'::text])),
    CONSTRAINT pcb_instances_fixed_chk CHECK (fixed IS NULL OR fixed = ANY (ARRAY['xy'::text, 'rot'::text, 'both'::text]))
);

COMMENT ON TABLE pcb_instances IS
    'A placement (refdes) of a component (ADR 0042 §4) — centroid x/y, rot '
    '(CW from north), layer, fixed, roles, note.';

CREATE UNIQUE INDEX IF NOT EXISTS pcb_instances_ref_refdes_key
    ON pcb_instances (ref_id, refdes) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS pcb_instances_ref_idx
    ON pcb_instances (ref_id) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS pcb_instances_component_idx
    ON pcb_instances (component_id);
CREATE INDEX IF NOT EXISTS pcb_instances_roles_gin
    ON pcb_instances USING gin (roles);

-- 5. pcb_nets ------------------------------------------------------
CREATE TABLE IF NOT EXISTS pcb_nets (
    net_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ref_id     bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    name       text   NOT NULL,                    -- REQUIRED + meaningful (the net's "meaning")
    net_class  text,                               -- signal|power|gnd|analog|i2c|spi|high_speed...
    est_current_a double precision,                -- worst-case current -> width
    width_mm   double precision,                   -- assigned width; NULL = derive (class default v IPC calc)
    note       text,                               -- LLM reasoning
    meta       jsonb  NOT NULL DEFAULT '{}',       -- layer hint, width override...
    retired_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE pcb_nets IS
    'PCB nets (ADR 0042 §4) — REQUIRED meaningful name (the net''s purpose), '
    'class, est current, derived width.';

CREATE UNIQUE INDEX IF NOT EXISTS pcb_nets_ref_name_key
    ON pcb_nets (ref_id, name) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS pcb_nets_ref_idx
    ON pcb_nets (ref_id) WHERE retired_at IS NULL;

-- 6. pcb_netconns — the netlist: (net, instance, pin) --------------
CREATE TABLE IF NOT EXISTS pcb_netconns (
    netconn_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    net_id       bigint NOT NULL REFERENCES pcb_nets (net_id) ON DELETE CASCADE,
    instance_id  bigint NOT NULL,
    pin_id       bigint NOT NULL,
    component_id bigint NOT NULL,                  -- denormalized so the FKs force pin.component = instance.component
    note         text,                             -- LLM reasoning about THIS connection
    meta         jsonb  NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (instance_id, component_id)
        REFERENCES pcb_instances (instance_id, component_id) ON DELETE CASCADE,
    FOREIGN KEY (pin_id, component_id)
        REFERENCES pcb_pins (pin_id, component_id) ON DELETE CASCADE
);

COMMENT ON TABLE pcb_netconns IS
    'The netlist (ADR 0042 §4): one row per (net, instance, pin). A physical '
    'pin is on at most one net. Composite FKs force pin.component = '
    'instance.component. note = why this wire. Hard-delete (re-wire = '
    'delete+insert).';

-- a physical pin (instance, pin) is on AT MOST ONE net
CREATE UNIQUE INDEX IF NOT EXISTS pcb_netconns_phys_pin_key
    ON pcb_netconns (instance_id, pin_id);
-- the graph hop
CREATE INDEX IF NOT EXISTS pcb_netconns_net_idx
    ON pcb_netconns (net_id);
CREATE INDEX IF NOT EXISTS pcb_netconns_instance_idx
    ON pcb_netconns (instance_id);

-- 7. pcb_measures — the measuring tapes (ADR 0042 §8.3) ------------
CREATE TABLE IF NOT EXISTS pcb_measures (
    measure_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ref_id     bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    metric     text   NOT NULL,                    -- separation|proximity|parallelism|supply_path|topology|plane_continuity|height|thermal
    direction  text,                               -- min|max|target|keep_above|keep_below
    goal       double precision,                   -- target/limit
    strength   text   NOT NULL DEFAULT 'gauge',    -- hard | soft | gauge
    weight     double precision,                   -- soft term weight; NULL = per-metric default
    operands   jsonb  NOT NULL,                    -- refs to instances/nets/classes/(instance,pin)
    reason     text,                               -- the intent
    meta       jsonb  NOT NULL DEFAULT '{}',
    retired_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pcb_measures_strength_chk CHECK (strength = ANY (ARRAY['hard'::text, 'soft'::text, 'gauge'::text]))
);

COMMENT ON TABLE pcb_measures IS
    'PCB measures (ADR 0042 §8.3) — the measuring tapes; hard/soft/gauge '
    'design intent over instances/nets/classes, re-evaluated on change.';

CREATE INDEX IF NOT EXISTS pcb_measures_ref_idx
    ON pcb_measures (ref_id) WHERE retired_at IS NULL;

-- 8. pcb_features — non-electrical placed geometry -----------------
CREATE TABLE IF NOT EXISTS pcb_features (
    feature_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ref_id     bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    ftype      text   NOT NULL,                    -- mounting_hole|fiducial|testpoint|keepout|outline
    x          double precision,
    y          double precision,
    rot        double precision NOT NULL DEFAULT 0,
    layer      text,                               -- top|bottom|all (NULL = through/all)
    fixed      text,
    geom       jsonb,                              -- hole diameter, keepout poly, outline path (mm)
    note       text,
    meta       jsonb  NOT NULL DEFAULT '{}',
    retired_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE pcb_features IS
    'Non-electrical placed features (ADR 0042 §4): mounting holes, fiducials, '
    'keepouts, the board outline.';

CREATE INDEX IF NOT EXISTS pcb_features_ref_idx
    ON pcb_features (ref_id) WHERE retired_at IS NULL;

-- 9. parts — Flow A bulk catalog (swapped wholesale; NO inbound FK) -
CREATE TABLE IF NOT EXISTS parts (
    lcsc       text PRIMARY KEY,                   -- "C25804"
    mfr        text,
    mfr_part   text,
    description text,
    jlcpcb_assemblable boolean NOT NULL DEFAULT false,
    basic      boolean NOT NULL DEFAULT false,     -- Basic vs Extended (feeder fee)
    stock      integer,
    price      jsonb,                              -- qty breaks
    package    text,                               -- 0402, QFN-32
    height_mm  double precision,
    params     jsonb,                              -- normalized parametrics
    datasheet_url text,
    description_tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('english'::regconfig, coalesce(description, ''))) STORED,
    refreshed_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE parts IS
    'LCSC/JLCPCB catalog (ADR 0042 §5, Flow A) — bulk from the jlcparts dump '
    'via staging + atomic swap. NO inbound FK (the swap drops the table).';

CREATE INDEX IF NOT EXISTS parts_select_idx
    ON parts (jlcpcb_assemblable, basic, stock DESC) WHERE jlcpcb_assemblable;
CREATE INDEX IF NOT EXISTS parts_tsv_gin
    ON parts USING gin (description_tsv);
CREATE INDEX IF NOT EXISTS parts_params_gin
    ON parts USING gin (params);

-- 10. part_footprints — Flow B lazy easyeda2kicad cache ------------
CREATE TABLE IF NOT EXISTS part_footprints (
    lcsc       text PRIMARY KEY,                   -- loose ref to parts.lcsc (no FK)
    pads       jsonb,                              -- pad geometry
    pin_map    jsonb,                              -- pad -> name + tags (materializes pcb_pins)
    courtyard  jsonb,                              -- bbox/polygon (mm)
    centroid   jsonb,                              -- pick-place point
    kicad_mod  text,                               -- converted footprint (for export)
    model_3d   text,                               -- optional 3D ref
    source     text,                               -- easyeda2kicad version
    fetched_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE part_footprints IS
    'easyeda2kicad footprint cache (ADR 0042 §5, Flow B) — lazy per selected '
    'part; keyed by C-number; never touched by the catalog swap.';

-- 11. part_availability — turnover signal (survives the swap) ------
CREATE TABLE IF NOT EXISTS part_availability (
    lcsc          text PRIMARY KEY,                -- loose ref to parts.lcsc (no FK)
    stock_now     integer,
    stock_prev    integer,
    ewma_stock    double precision,
    restock_count integer NOT NULL DEFAULT 0,      -- times stock rose between dumps
    last_restock_at timestamptz,
    trend         double precision,                -- <0 only-draining = last-reel risk
    first_seen    timestamptz NOT NULL DEFAULT now(),
    discontinued  boolean NOT NULL DEFAULT false,  -- absent from latest dump
    updated_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE part_availability IS
    'Per-part turnover signal (ADR 0042 §5) — diffed from daily dumps; '
    'survives the catalog swap; selection ranks on this, not live stock.';

COMMIT;

-- End of 0042_pcb_kind.sql

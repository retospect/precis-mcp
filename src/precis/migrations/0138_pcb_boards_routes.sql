-- 0138_pcb_boards_routes.sql
--
-- docs/backlog/pcb-guided-place-route.md Slice 1 — the schema hedges for
-- the LLM-guided topological place+route story: netlist != board (multi-
-- board systems), stackup-as-data (flex / aluminum / rigid-flex), fold
-- lines as geometry, and domain-typed nets with class-driven router rules
-- (microfluidic / thermal co-design). v1 behavior stays one rigid 4-layer
-- electrical board — the hedges are schema shape only.
--
-- New tables:
--   * pcb_boards       — one physical board per design (v1: exactly one,
--                        name 'main'); `stackup` is the ordered layer
--                        array, `fold_lines` empty in v1.
--   * pcb_net_classes  — per-design router/DRC rules, joined by
--                        pcb_nets.net_class = name; missing row = built-in
--                        defaults, the router/DRC never assume copper.
--   * pcb_routes       — the canonical sketch (tree/topology/layer_assign)
--                        per (board, net); status is the legible route
--                        state machine; `fail` carries the legible failure
--                        (blocking gap, participants, clearance math).
--   * pcb_copper       — DERIVED, regenerable realized geometry (tracks/
--                        vias/pours); regenerated wholesale (DELETE+INSERT)
--                        per realize run, same discipline as chunks; no
--                        retired_at (never soft-deleted, only replaced).
--   * pcb_planes       — authored plane assignment per (board, layer, net).
--   * pcb_drc_findings — durable, linkable DRC results per (board, run_id).
--
-- Changed tables:
--   * pcb_instances / pcb_features — `board_id` added (FK -> pcb_boards),
--     backfilled to each design's one 'main' board, then NOT NULL. Nets and
--     netconns get NO board column (ADR: the netlist layer never
--     references geometry; a net spans boards through connector mate
--     links, `meta.mates_with` on instances, in v1).
--   * pcb_nets — `domain` added (electrical|fluidic|thermal, DEFAULT
--     'electrical'); v1 rejects non-electrical at the handler with a clear
--     message (schema-reserved, not yet routed).
--
-- The default stackup literal below is the single JSON source of truth
-- shared with `precis.pcb.DEFAULT_STACKUP` (Python side) — keep the two in
-- sync by eye; dielectric detail (material/thickness_mm) is schema-legal
-- but out of v1's default (roles only).
--
-- squawk: shipped with PRECIS_SQUAWK=0, deliberately. squawk flags the two
-- FK ADD COLUMNs, the two SET NOT NULLs, and the domain CHECK as scan/lock
-- hazards. Verified at ship time (2026-08-27) that pcb_instances,
-- pcb_features and pcb_nets each held **0 rows** in prod (0 pcb designs
-- exist), so every scan is instant and no lock is held meaningfully.
-- Squawk's suggested workaround would also make the schema strictly worse
-- here — a nullable board_id + CHECK instead of the real NOT NULL this
-- hedge depends on. If these tables are ever non-trivial, a FUTURE
-- migration touching them must use NOT VALID + VALIDATE rather than copy
-- this one's shape.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

-- 1. pcb_boards — one physical board per design ----------------------
CREATE TABLE IF NOT EXISTS pcb_boards (
    board_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ref_id     bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    name       text   NOT NULL DEFAULT 'main',
    -- ordered layer array: [{name, role: signal|plane|dielectric|
    -- stiffener, material?, thickness_mm?, plane_net?}]; v1 default is the
    -- 4-layer rigid FR-4 SIG/GND/PWR/SIG template (roles only).
    stackup    jsonb  NOT NULL,
    fold_lines jsonb  NOT NULL DEFAULT '[]',  -- flex fold geometry; empty in v1
    note       text,
    meta       jsonb  NOT NULL DEFAULT '{}',
    retired_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE pcb_boards IS
    'A physical board of a design (pcb-guided-place-route Slice 1) — '
    'stackup as ordered jsonb (boards are few, stackups read as a unit); '
    'fold_lines geometry (empty in v1, flex/rigid-flex hedge). v1: exactly '
    'one board per design, name ''main''.';

CREATE UNIQUE INDEX IF NOT EXISTS pcb_boards_ref_name_key
    ON pcb_boards (ref_id, name) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS pcb_boards_ref_idx
    ON pcb_boards (ref_id) WHERE retired_at IS NULL;
-- the retired_at-filtered partial above doesn't cover an unqualified
-- FK-cascade lookup (test_schema_design.py convention, 0136) — a plain
-- index alongside it does.
CREATE INDEX IF NOT EXISTS pcb_boards_ref_id_fk_idx
    ON pcb_boards (ref_id);

-- 2. board_id on pcb_instances / pcb_features -------------------------
ALTER TABLE pcb_instances
    ADD COLUMN IF NOT EXISTS board_id bigint REFERENCES pcb_boards (board_id) ON DELETE CASCADE;
ALTER TABLE pcb_features
    ADD COLUMN IF NOT EXISTS board_id bigint REFERENCES pcb_boards (board_id) ON DELETE CASCADE;

-- backfill: one 'main' board per existing pcb-kind design ref, then point
-- every existing instance/feature row at its design's board.
INSERT INTO pcb_boards (ref_id, name, stackup)
SELECT r.ref_id, 'main',
       '[{"name":"F.Cu","role":"signal"},'
       '{"name":"In1.Cu","role":"plane","plane_net":"GND"},'
       '{"name":"In2.Cu","role":"plane"},'
       '{"name":"B.Cu","role":"signal"}]'::jsonb
FROM refs r
WHERE r.kind = 'pcb'
ON CONFLICT (ref_id, name) WHERE retired_at IS NULL DO NOTHING;

UPDATE pcb_instances i
SET board_id = b.board_id
FROM pcb_boards b
WHERE b.ref_id = i.ref_id AND b.name = 'main' AND i.board_id IS NULL;

UPDATE pcb_features f
SET board_id = b.board_id
FROM pcb_boards b
WHERE b.ref_id = f.ref_id AND b.name = 'main' AND f.board_id IS NULL;

ALTER TABLE pcb_instances ALTER COLUMN board_id SET NOT NULL;
ALTER TABLE pcb_features ALTER COLUMN board_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS pcb_instances_board_idx ON pcb_instances (board_id);
CREATE INDEX IF NOT EXISTS pcb_features_board_idx ON pcb_features (board_id);

-- 3. pcb_nets.domain — electrical | fluidic | thermal ------------------
ALTER TABLE pcb_nets ADD COLUMN IF NOT EXISTS domain text NOT NULL DEFAULT 'electrical';

ALTER TABLE pcb_nets DROP CONSTRAINT IF EXISTS pcb_nets_domain_chk;
ALTER TABLE pcb_nets ADD CONSTRAINT pcb_nets_domain_chk
    CHECK (domain = ANY (ARRAY['electrical'::text, 'fluidic'::text, 'thermal'::text]));

COMMENT ON COLUMN pcb_nets.domain IS
    'electrical|fluidic|thermal (pcb-guided-place-route hedge). v1 routes '
    'electrical only; the handler rejects fluidic/thermal at put with a '
    'clear message — the column is schema-reserved for later co-design.';

-- 4. pcb_net_classes — per-design router/DRC rules ---------------------
CREATE TABLE IF NOT EXISTS pcb_net_classes (
    class_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ref_id     bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    name       text   NOT NULL,             -- joins pcb_nets.net_class
    -- clearance_mm, track width, via drill/annular, permitted layers,
    -- length-match group, domain defaults. Reserved fields (length-match /
    -- diff-pair) are read but not enforced in v1.
    rules      jsonb  NOT NULL DEFAULT '{}',
    note       text,
    meta       jsonb  NOT NULL DEFAULT '{}',
    retired_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE pcb_net_classes IS
    'Per-design net-class rules (pcb-guided-place-route Slice 1) — joined '
    'by pcb_nets.net_class = name; a missing row means built-in defaults. '
    'The router/DRC read rules only from here, never assume copper.';

CREATE UNIQUE INDEX IF NOT EXISTS pcb_net_classes_ref_name_key
    ON pcb_net_classes (ref_id, name) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS pcb_net_classes_ref_idx
    ON pcb_net_classes (ref_id) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS pcb_net_classes_ref_id_fk_idx
    ON pcb_net_classes (ref_id);

-- 5. pcb_routes — the canonical sketch, one per (board, net) -----------
CREATE TABLE IF NOT EXISTS pcb_routes (
    route_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    board_id     bigint NOT NULL REFERENCES pcb_boards (board_id) ON DELETE CASCADE,
    net_id       bigint NOT NULL REFERENCES pcb_nets (net_id) ON DELETE CASCADE,
    tree         jsonb,                     -- two-pin connection decomposition incl. Steiner/via points
    topology     jsonb,                     -- per-connection ordered (anchor, side) list
    layer_assign jsonb,
    status       text   NOT NULL DEFAULT 'unrouted',
    -- the legible failure: blocking gap, participants, clearance arithmetic
    fail         jsonb,
    note         text,
    meta         jsonb  NOT NULL DEFAULT '{}',
    updated_at   timestamptz NOT NULL DEFAULT now(),
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pcb_routes_status_chk CHECK (
        status = ANY (ARRAY['unrouted'::text, 'sketched'::text, 'realized'::text, 'failed'::text])
    )
);

COMMENT ON TABLE pcb_routes IS
    'The canonical sketch (pcb-guided-place-route) — sketch-as-canonical, '
    'copper is derived (pcb_copper). One row per (board, net); status is '
    'the legible route state machine; fail names the blocking gap.';

CREATE UNIQUE INDEX IF NOT EXISTS pcb_routes_board_net_key
    ON pcb_routes (board_id, net_id);
CREATE INDEX IF NOT EXISTS pcb_routes_board_idx
    ON pcb_routes (board_id);
-- net_id isn't a leading prefix of the (board_id, net_id) unique index —
-- its own FK-cascade coverage.
CREATE INDEX IF NOT EXISTS pcb_routes_net_id_fk_idx
    ON pcb_routes (net_id);

-- 6. pcb_copper — DERIVED, regenerable realized geometry ----------------
CREATE TABLE IF NOT EXISTS pcb_copper (
    copper_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    board_id     bigint NOT NULL REFERENCES pcb_boards (board_id) ON DELETE CASCADE,
    ctype        text   NOT NULL,           -- track | via | pour
    layer        text   NOT NULL,
    net_id       bigint NOT NULL REFERENCES pcb_nets (net_id) ON DELETE CASCADE,
    route_id     bigint REFERENCES pcb_routes (route_id) ON DELETE CASCADE,
    -- polyline+width (track) | pos/drill (via) | pour polygon, mm
    geom         jsonb  NOT NULL,
    generated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pcb_copper_ctype_chk CHECK (
        ctype = ANY (ARRAY['track'::text, 'via'::text, 'pour'::text])
    )
);

COMMENT ON TABLE pcb_copper IS
    'DERIVED realized copper (pcb-guided-place-route) — regenerated '
    'wholesale (DELETE board''s rows + INSERT) per realize run, the same '
    'cascade discipline as chunks->embeddings. Never hand-edited, no '
    'retired_at — a realize run replaces, it does not soft-delete.';

CREATE INDEX IF NOT EXISTS pcb_copper_board_idx
    ON pcb_copper (board_id);
CREATE INDEX IF NOT EXISTS pcb_copper_net_id_fk_idx
    ON pcb_copper (net_id);
CREATE INDEX IF NOT EXISTS pcb_copper_route_id_fk_idx
    ON pcb_copper (route_id);

-- 7. pcb_planes — authored plane assignment per (board, layer, net) -----
CREATE TABLE IF NOT EXISTS pcb_planes (
    plane_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    board_id    bigint NOT NULL REFERENCES pcb_boards (board_id) ON DELETE CASCADE,
    layer       text   NOT NULL,
    net_id      bigint NOT NULL REFERENCES pcb_nets (net_id) ON DELETE CASCADE,
    region_hint jsonb,
    note        text,
    meta        jsonb  NOT NULL DEFAULT '{}',
    retired_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE pcb_planes IS
    'Authored plane assignment (pcb-guided-place-route) per (board, layer, '
    'net) + region_hint. Derived polygon + island report live in '
    'pcb_copper (ctype=pour) + pcb_drc_findings.';

CREATE UNIQUE INDEX IF NOT EXISTS pcb_planes_board_layer_net_key
    ON pcb_planes (board_id, layer, net_id) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS pcb_planes_board_idx
    ON pcb_planes (board_id) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS pcb_planes_board_id_fk_idx
    ON pcb_planes (board_id);
-- net_id isn't a leading prefix of the (board_id, layer, net_id) partial
-- unique index — its own FK-cascade coverage.
CREATE INDEX IF NOT EXISTS pcb_planes_net_id_fk_idx
    ON pcb_planes (net_id);

-- 8. pcb_drc_findings — durable, linkable DRC results --------------------
CREATE TABLE IF NOT EXISTS pcb_drc_findings (
    finding_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    board_id   bigint NOT NULL REFERENCES pcb_boards (board_id) ON DELETE CASCADE,
    run_id     text   NOT NULL,
    rule       text   NOT NULL,
    severity   text   NOT NULL,             -- error | warn
    objects    jsonb  NOT NULL DEFAULT '[]',
    detail     text,
    waived_by  text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pcb_drc_findings_severity_chk CHECK (
        severity = ANY (ARRAY['error'::text, 'warn'::text])
    )
);

COMMENT ON TABLE pcb_drc_findings IS
    'Durable, linkable DRC results (pcb-guided-place-route) per (board, '
    'run_id) — gate evaluators and the LLM read the latest run.';

CREATE INDEX IF NOT EXISTS pcb_drc_findings_board_run_idx
    ON pcb_drc_findings (board_id, run_id);

COMMIT;

-- End of 0138_pcb_boards_routes.sql

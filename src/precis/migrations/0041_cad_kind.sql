-- 0041_cad_kind.sql
--
-- ADR 0041 (+ Amendment 1, 2026-06-28) — the `cad` kind: a parametric
-- solid-model design as a first-class ref. A design is a boolean DAG of
-- placed analytic primitives (the kernel lives in `precis.cad`); the
-- agent *probes* it (point / ray / arc / section) and *relates* whole
-- parts (clearance / interference / translational DOF) rather than
-- meshing. Postgres is canonical; OpenSCAD/STL export is downstream.
--
-- Storage (Amendment 1): split by what is actually a search target.
--   * The DESIGN is a `ref` (kind='cad', slug, links, tags); design-level
--     metadata (units, tolerances) lives on `refs.meta`.
--   * The design keeps ONE `card_combined` chunk (the existing card kind,
--     ord<0) carrying an auto-built summary — title + component + node
--     names + shapes + bbox — so `search(kind='cad', q=…)` works on
--     intent and joins the cross-kind embedding search. One vector per
--     design; the geometry never touches the embedding DB.
--   * The NODES live in the dedicated `cad_nodes` table below — structured
--     geometry, never embedded / keyworded / salience-rotated (the chunk
--     indexers claim by a kind-blind derived-queue join, so chunk storage
--     would earn every frustum a 1024-d vector nobody searches for).
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

-- 1. the ref kind ----------------------------------------------------
INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('cad', FALSE, 'CAD',
     'Parametric solid-model design (ADR 0041) — a boolean DAG of placed '
     'analytic primitives (box/cyl/cone/sphere/torus/prism/pyramid) '
     'authored via the compact `config` mini-DSL (e.g. cyl:r3h12). '
     'Postgres-canonical; the agent probes the model '
     '(point/ray/arc/section) and relates whole parts '
     '(clearance/interference/translational DOF) analytically rather than '
     'meshing. OpenSCAD/STL export is a regenerable downstream view. '
     'Named ref; nodes addressed by an opaque ca<id> handle. '
     'See precis-cad-help.')
ON CONFLICT (slug) DO NOTHING;

-- 2. the node table — the design DAG, never embedded -----------------
CREATE TABLE IF NOT EXISTS cad_nodes (
    node_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ref_id     bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    ord        integer NOT NULL,                 -- author / eval order
    name       text NOT NULL,                    -- per-design unique label
    component  text NOT NULL,                    -- owning physical part
    op         text NOT NULL,                    -- add | cut | intersect
    config     text NOT NULL,                    -- the mini-DSL shape string
    loc        double precision[] NOT NULL DEFAULT '{0,0,0}',
    rot        double precision[] NOT NULL DEFAULT '{0,0,0}',
    pattern    jsonb,                             -- polar/linear sugar, or NULL
    operands   bigint[],                          -- explicit DAG (forward-compat)
    retired_at timestamptz,                       -- ADR 0033 soft-delete
    created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE cad_nodes IS
    'CAD design nodes (ADR 0041 Amendment 1): one placed primitive / '
    'boolean operator per row, owned by a kind=cad ref. Structured '
    'geometry — never embedded; probes fold these on demand.';

-- a node name is unique within its (live) design
CREATE UNIQUE INDEX IF NOT EXISTS cad_nodes_ref_name_key
    ON cad_nodes (ref_id, name) WHERE retired_at IS NULL;
-- the hot read: a design's live nodes in author order
CREATE INDEX IF NOT EXISTS cad_nodes_ref_ord_idx
    ON cad_nodes (ref_id, ord) WHERE retired_at IS NULL;

COMMIT;

-- End of 0041_cad_kind.sql

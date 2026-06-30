-- 0042_structure_kind.sql
--
-- ADR 0043 — the `structure` kind: a legible atomistic cell + bond-graph IR
-- the LLM reads as graph + numbers, not pixels (the materials sibling of
-- `cad` (0041) / `pcb` (0042-on-elecad)). A design is a periodic cell
-- (lattice + per-axis PBC on `refs.meta`) filled with atoms and an explicit
-- bond graph; the agent edits the graph (intent) and *probes* it analytically
-- (neighbours / coordination / distances / the validator gate) in memory.
-- The relaxer/DFT and file I/O are rented backends added later.
--
-- Storage (ADR 0043 §4/§12), same split as `cad` Amendment 1:
--   * the DESIGN is a slug-addressed `refs` row (kind='structure'); the cell
--     (lattice 3×3, pbc, version, label high-water) lives on `refs.meta`;
--   * it keeps ONE `card_combined` chunk (composition + intent) so
--     search(kind='structure', q=…) works on intent — one vector per design;
--   * the GRAPH lives in the dedicated `struct_*` tables below — structured,
--     never embedded (the chunk indexers are kind-blind, so chunk storage
--     would earn every atom a 1024-d vector nobody searches for).
--
-- Per-atom DERIVED outputs (force/charge) are run-scoped and live with the
-- run (a later increment), never on the mutable atom row (§12 fix #1).
--
-- Forward-only (ADR 0005). Idempotent. Handle code: `st` (STructure).

BEGIN;

-- 1. the ref kind ----------------------------------------------------
INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('structure', FALSE, 'Structure',
     'Atomistic cell + bond-graph design for DFT/molecular modelling '
     '(ADR 0043). A periodic cell (lattice + per-axis PBC) filled with atoms '
     '(a<El><n> labels) and an explicit bond graph (order + provenance + '
     'periodic-image offset). The agent edits the graph via typed ops and '
     'probes it analytically (neighbours, coordination, MIC distances/angles, '
     'a validator gate) in memory — never pixels. Relaxation/DFT and file '
     'export (CIF/POSCAR/XYZ) are rented backends. Postgres-canonical; '
     'st<id> handle, design-scoped atom paths st<id>#a<El><n>. '
     'See precis-structure-help.')
ON CONFLICT (slug) DO NOTHING;

-- 2. atoms — the design's atoms (intent + current fractional position) ----
CREATE TABLE IF NOT EXISTS struct_atoms (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ref_id          bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    label           text NOT NULL,            -- 'aPd123', design-scoped
    element         text NOT NULL,
    fa              double precision NOT NULL,  -- fractional position
    fb              double precision NOT NULL,
    fc              double precision NOT NULL,
    fixed           smallint NOT NULL DEFAULT 0,  -- bitmask bit0=x bit1=y bit2=z
    magmom          double precision,           -- declared intent
    oxidation       smallint,                   -- declared intent
    hybridization   text,                       -- declared intent only
    added_version   integer NOT NULL,
    retired_version integer,                     -- NULL = live
    created_at      timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE struct_atoms IS
    'ADR 0043 §4/§12: a design''s atoms — intent + current fractional '
    'position. Per-atom DERIVED outputs (force/charge) are run-scoped, not '
    'here. Never embedded.';
-- a label is unique within the live design
CREATE UNIQUE INDEX IF NOT EXISTS struct_atoms_ref_label_key
    ON struct_atoms (ref_id, label) WHERE retired_version IS NULL;
CREATE INDEX IF NOT EXISTS struct_atoms_ref_element_idx
    ON struct_atoms (ref_id, element) WHERE retired_version IS NULL;

-- 3. bonds — the editable graph (pairwise inline; N-ary via members) ------
CREATE TABLE IF NOT EXISTS struct_bonds (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ref_id          bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    kind            text NOT NULL DEFAULT 'pairwise',  -- pairwise|aromatic|eta-n|3c2e
    bond_order      real NOT NULL DEFAULT 1.0,         -- vdW(≈0)…single…metallic
    provenance      text NOT NULL DEFAULT 'declared',  -- declared|inferred|dft
    i               bigint REFERENCES struct_atoms (id) ON DELETE CASCADE,
    j               bigint REFERENCES struct_atoms (id) ON DELETE CASCADE,
    image           integer[] NOT NULL DEFAULT '{0,0,0}',  -- to_jimage on j
    added_version   integer NOT NULL,
    retired_version integer,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS struct_bonds_ref_i_idx
    ON struct_bonds (ref_id, i) WHERE retired_version IS NULL;
CREATE INDEX IF NOT EXISTS struct_bonds_ref_j_idx
    ON struct_bonds (ref_id, j) WHERE retired_version IS NULL;

-- 4. N-ary bond membership (ring / η-n / 3c2e) ---------------------------
CREATE TABLE IF NOT EXISTS struct_bond_atoms (
    bond_id  bigint NOT NULL REFERENCES struct_bonds (id) ON DELETE CASCADE,
    atom_id  bigint NOT NULL REFERENCES struct_atoms (id) ON DELETE CASCADE,
    image    integer[] NOT NULL DEFAULT '{0,0,0}',
    PRIMARY KEY (bond_id, atom_id)
);

-- 5. measures / observers / cursors (persisted, re-evaluated) ------------
CREATE TABLE IF NOT EXISTS struct_measures (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ref_id          bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    kind            text NOT NULL,             -- bond_length|coordination|…|cursor
    direction       text,                      -- min|max|target|… (NULL for a cursor)
    goal            jsonb,
    strength        text NOT NULL DEFAULT 'gauge',  -- hard|soft|gauge
    operands        jsonb,
    embodiment      jsonb,                     -- (anchor, selector, reach) spec
    anchor_atom_id  bigint REFERENCES struct_atoms (id) ON DELETE CASCADE,
    anchor_bond_id  bigint REFERENCES struct_bonds (id) ON DELETE CASCADE,
    "for"           text,                      -- cursor purpose
    value_derived   jsonb,
    verdict         text,
    retired_version integer,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS struct_measures_ref_idx
    ON struct_measures (ref_id) WHERE retired_version IS NULL;

COMMIT;

-- End of 0042_structure_kind.sql

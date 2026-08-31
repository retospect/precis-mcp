-- precis_nm/0001_nm_kind.sql
--
-- The `nm` (nanomachine) kind (docs/backlog/nm-kind.md) — the fourth
-- keystone kind, sibling to cad (ADR 0041) / pcb (0042) / structure (0043):
-- a hierarchical molecular-machine design as a slug-addressed ref. A design
-- is a **block tree** — nested building blocks (a disc, a fork, an axle),
-- each with a spatial envelope (the `precis.cad.dsl` mini-DSL, Angstrom
-- units throughout — see nm-kind.md's "Decisions") and optional declared
-- DOF; blocks reuse-by-reference via `template_block_id` (instance a sugar
-- once, place it seven times — `cad`'s `Design.instance` pattern, resolved
-- at read time, never copied). Ports (per-block attachment points) and
-- topology invariants (threading/chirality, L2) are slice-3-round-2/3 —
-- their tables are created now (forward-only discipline: no later ALTER)
-- but unused until the handler wires them up.
--
-- Storage (the 0041 rule, transferred verbatim): the design keeps ONE
-- `card_combined` chunk (title + description + block names + desc/use
-- texts) so `search(kind='nm', q=…)` works on intent and joins the
-- cross-kind embedding search — one vector per design. The block/port/
-- topology graph lives in the dedicated tables below, never chunks: a
-- block row isn't a search target on its own, and folding thousands of
-- blocks through the chunk indexer's kind-blind derived-queue join would
-- earn every one an unwanted embedding nobody queries for.
--
-- Ships DARK behind the `nm.enabled` setting (KindSpec.requires_setting;
-- DB row → PRECIS_NM_ENABLED env → unset/off, the chem.enabled pattern) —
-- seeding the kind row is inert until the flag turns the kind on.
--
-- Forward-only (ADR 0005). Idempotent. This is a PLUGIN migration
-- (namespace `precis_nm`), applied after core via Migrator.discover_sources
-- — so the `kinds` reference table + the `refs` table already exist.

BEGIN;

-- 1. the ref kind ------------------------------------------------------
INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('nm', FALSE, 'Nanomachine',
     'A hierarchical molecular-machine design (docs/backlog/nm-kind.md, '
     'the fourth keystone kind): nested building blocks (a disc, a fork, '
     'an axle — `nm_blocks`) each with a spatial envelope (the cad '
     'mini-DSL, Angstrom units) and optional declared DOF, reusable by '
     'reference (instance a block once, place it many times). Ports and '
     'topology invariants (threading/chirality) layer on top. The LLM '
     'traverses a block tree, never atoms directly — real chemistry fills '
     'the envelopes in later slices via linked `structure` designs.')
ON CONFLICT (slug) DO NOTHING;

-- 2. the block tree ------------------------------------------------------
CREATE TABLE IF NOT EXISTS nm_blocks (
    id                 bigserial PRIMARY KEY,
    ref_id             bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    -- tree edge (assembly nesting) — self-FK, nullable = root block.
    parent_block_id    bigint REFERENCES nm_blocks (id),
    -- reuse-by-reference edge (instancing) — self-FK, nullable = an
    -- ordinary (non-instance) block. Resolved at read time: an instance's
    -- subtree is the template's, never copied.
    template_block_id  bigint REFERENCES nm_blocks (id),
    name               text NOT NULL,
    -- pose (Angstrom) + rotation (degrees), float64 throughout (no
    -- fixed-point — see nm-kind.md's "Decisions": rotations produce
    -- irrational coordinates regardless, and quantization belongs only at
    -- the hash boundary, structure/canonical.py's pattern).
    pose_xyz           double precision[] NOT NULL DEFAULT '{0,0,0}',
    pose_rot           double precision[] NOT NULL DEFAULT '{0,0,0}',
    -- the cad mini-DSL config string (e.g. 'cyl:r5h2'), NULL = no declared
    -- envelope yet (a pure hypergraph node, L0).
    envelope           text,
    descr              text,
    use_               text,
    -- declared DOF, e.g. {"kind":"rotational","axis_ports":[...]} — free
    -- passthrough at this round, no schema enforced yet.
    dof                jsonb,
    retired_at         timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE nm_blocks IS
    'nm block tree (docs/backlog/nm-kind.md slice 3): one nested building '
    'block per row, owned by a kind=nm ref. Structured graph — never '
    'embedded; the design''s one card_combined chunk carries the '
    'searchable summary.';

-- a block name is unique within its (live) design — simpler addressing
-- than per-parent scoping, mirrors cad_nodes_ref_name_key.
CREATE UNIQUE INDEX IF NOT EXISTS nm_blocks_ref_name_key
    ON nm_blocks (ref_id, name) WHERE retired_at IS NULL;
-- the hot reads: a design's live blocks, and the tree walk by parent.
CREATE INDEX IF NOT EXISTS nm_blocks_ref_idx
    ON nm_blocks (ref_id) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS nm_blocks_parent_idx
    ON nm_blocks (parent_block_id) WHERE retired_at IS NULL;

-- 3. ports — per-block attachment points (round 2; created now, unused) --
CREATE TABLE IF NOT EXISTS nm_ports (
    id                      bigserial PRIMARY KEY,
    block_id                bigint NOT NULL REFERENCES nm_blocks (id) ON DELETE CASCADE,
    name                    text NOT NULL,
    -- capability set (the pin→roles pattern, transferred from
    -- pcb-component-model.md): legal attachments are derived at bind time
    -- from these roles, never stored as a second relation.
    roles                   text[] NOT NULL DEFAULT '{}',
    direction               double precision[],
    expected_element        text,
    expected_hybridization  text,
    -- the atom-side projection of the one port fact (structure slug +
    -- atom label), NULL until filled.
    bound_design            text,
    bound_atom              text,
    retired_at              timestamptz,
    created_at              timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE nm_ports IS
    'nm port metadata (docs/backlog/nm-kind.md slice 3 round 2): a named '
    'attachment point on a block — envelope-side stub now, bound to a real '
    'structure atom once filled. Unused until the port ops land.';

CREATE UNIQUE INDEX IF NOT EXISTS nm_ports_block_name_key
    ON nm_ports (block_id, name) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS nm_ports_block_idx
    ON nm_ports (block_id) WHERE retired_at IS NULL;

-- 4. topology — L2 invariants, stored explicitly (round 3; unused) ------
CREATE TABLE IF NOT EXISTS nm_topology (
    id             bigserial PRIMARY KEY,
    ref_id         bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    kind           text NOT NULL CHECK (kind IN ('threading', 'chirality')),
    subject_block  bigint NOT NULL REFERENCES nm_blocks (id),
    object_block   bigint REFERENCES nm_blocks (id),
    meta           jsonb,
    retired_at     timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE nm_topology IS
    'nm L2 topology invariants (docs/backlog/nm-kind.md slice 3 round 3): '
    'mechanical interlocking (threading) and chirality marks, stored '
    'explicitly — never re-derived from L3 coordinates. Unused until the '
    'declare_threading/declare_chirality ops land.';

CREATE INDEX IF NOT EXISTS nm_topology_ref_idx
    ON nm_topology (ref_id) WHERE retired_at IS NULL;

COMMIT;

-- End of 0001_nm_kind.sql

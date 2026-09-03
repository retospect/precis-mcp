-- precis_se/0001_se_kind.sql
--
-- The `se` (structural envelope) kind (docs/backlog/se-kind.md) — the
-- scale-agnostic sibling of `nm` (se : cad :: nm : structure): a
-- macro-scale space-planner design as a slug-addressed ref. A design is a
-- **block tree** — nested building blocks (a fork, a hub, a wheel), each
-- with a spatial envelope (the `precis.cad.dsl` mini-DSL, METRES
-- throughout, float64 — see se-kind.md's "Decisions") and rough pose;
-- blocks reuse-by-reference via `template_block_id` (`cad`'s
-- `Design.instance` pattern, resolved at read time, never copied), and
-- **arrays are first-class**: an array node is an instance node with a
-- multiplicity spec (`array_spec` — the cad text language's
-- `linear:`/`polar:` node modifiers lifted to block level). Ports
-- (annotated attachment points) and connects (joint kinematic class ×
-- mechanism) are the next round — their tables are created now
-- (forward-only discipline: no later ALTER) but unused until the handler
-- wires them up. Measures/BOM/notes tables land with their own slices.
--
-- Storage (the 0041 rule, transferred verbatim): the design keeps ONE
-- `card_combined` chunk (title + description + block names + desc/use
-- texts) so `search(kind='se', q=…)` works on intent and joins the
-- cross-kind embedding search — one vector per design. The block/port/
-- connect graph lives in the dedicated tables below, never chunks.
--
-- Ships DARK behind the `se.enabled` setting (KindSpec.requires_setting;
-- DB row → PRECIS_SE_ENABLED env → unset/off, the nm.enabled pattern) —
-- seeding the kind row is inert until the flag turns the kind on.
--
-- Forward-only (ADR 0005). Idempotent. This is a PLUGIN migration
-- (namespace `precis_se`), applied after core via Migrator.discover_sources
-- — so the `kinds` reference table + the `refs` table already exist.

BEGIN;

-- 1. the ref kind ------------------------------------------------------
INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('se', FALSE, 'Structural envelope',
     'A scale-agnostic structural/space-planner design '
     '(docs/backlog/se-kind.md, sibling of nm at macro scale): nested '
     'building blocks (`se_blocks`) each with a spatial envelope (the cad '
     'mini-DSL, metres) and rough pose, reusable by reference (instance a '
     'block once, place it many times) with first-class linear/polar '
     'arrays. Ports, joints, tolerances and manufacturing modes layer on '
     'in later slices. The LLM traverses a block tree, never raw '
     'geometry.')
ON CONFLICT (slug) DO NOTHING;

-- 2. the block tree ------------------------------------------------------
CREATE TABLE IF NOT EXISTS se_blocks (
    id                 bigserial PRIMARY KEY,
    ref_id             bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    -- tree edge (assembly nesting) — self-FK, nullable = root block.
    parent_block_id    bigint REFERENCES se_blocks (id),
    -- reuse-by-reference edge (instancing AND arrays) — self-FK, nullable
    -- = an ordinary block. Resolved at read time: an instance's subtree is
    -- the template's, never copied; an array member's solid is the
    -- template's under that member's transform.
    template_block_id  bigint REFERENCES se_blocks (id),
    name               text NOT NULL,
    -- pose (METRES) + rotation (degrees), float64 throughout (se-kind.md
    -- "Decisions": float64 metres everywhere; exact equality only ever at
    -- a hash boundary, never in the representation).
    pose_xyz           double precision[] NOT NULL DEFAULT '{0,0,0}',
    pose_rot           double precision[] NOT NULL DEFAULT '{0,0,0}',
    -- the cad mini-DSL config string (e.g. 'cyl:r0.02h0.01'), metres.
    -- NULL = no declared envelope yet (a pure L0 node — suggestive by
    -- contract: absence is reported, never failed).
    envelope           text,
    -- array-instance spec (NULL = not an array): {"kind":"linear",
    -- "count":N,"pitch":p,"axis":[x,y,z]} or {"kind":"polar","count":N,
    -- "radius":r,"axis":[x,y,z]}, plus later per-member "overrides".
    -- Only ever set together with template_block_id.
    array_spec         jsonb,
    descr              text,
    use_               text,
    -- manufacturing mode (slice 5+): 'fdm/asa'-style key into the
    -- capability rows. NULL = unassigned, which is honest.
    mode               text,
    -- build frame (slice 5+): print orientation with its own "down",
    -- distinct from the working frame.
    build_frame        jsonb,
    -- L3 realization binding (slice 5+): a cad or nm design slug.
    bound_kind         text CHECK (bound_kind IN ('cad', 'nm')),
    bound_design       text,
    retired_at         timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE se_blocks IS
    'se block tree (docs/backlog/se-kind.md): one nested building block '
    'per row, owned by a kind=se ref. Structured graph — never embedded; '
    'the design''s one card_combined chunk carries the searchable summary.';

-- a block name is unique within its (live) design — simpler addressing
-- than per-parent scoping, mirrors nm_blocks_ref_name_key.
CREATE UNIQUE INDEX IF NOT EXISTS se_blocks_ref_name_key
    ON se_blocks (ref_id, name) WHERE retired_at IS NULL;
-- the hot reads: a design's live blocks, the tree walk by parent, the
-- instance/array template edge. Plain (non-partial) so they also cover
-- FK-cascade scans (test_schema_design's covering-index rule: a
-- retired_at-partial index doesn't serve `col = $1` over retired rows).
CREATE INDEX IF NOT EXISTS se_blocks_ref_idx
    ON se_blocks (ref_id);
CREATE INDEX IF NOT EXISTS se_blocks_parent_idx
    ON se_blocks (parent_block_id);
CREATE INDEX IF NOT EXISTS se_blocks_template_idx
    ON se_blocks (template_block_id);

-- 3. ports — annotated attachment points (next round; created now, unused)
CREATE TABLE IF NOT EXISTS se_ports (
    id           bigserial PRIMARY KEY,
    block_id     bigint NOT NULL REFERENCES se_blocks (id) ON DELETE CASCADE,
    name         text NOT NULL,
    -- capability set (the pin→roles pattern): legal attachments are
    -- derived at connect time from these roles, never stored twice.
    roles        text[] NOT NULL DEFAULT '{}',
    direction    double precision[],
    -- open dict over the ONE superset annotation registry (se-kind.md
    -- "Annotations"): registered keys with a contract class (checked vs
    -- descriptive); storage is transport, semantics live in the consumers.
    annotations  jsonb,
    retired_at   timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE se_ports IS
    'se port metadata (docs/backlog/se-kind.md): a named attachment point '
    'on a block, with capability roles and superset-registry annotations. '
    'Unused until the port ops land.';

CREATE UNIQUE INDEX IF NOT EXISTS se_ports_block_name_key
    ON se_ports (block_id, name) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS se_ports_block_idx
    ON se_ports (block_id);

-- 4. connects — port↔port intent edges (next round; created now, unused) --
CREATE TABLE IF NOT EXISTS se_connects (
    id          bigserial PRIMARY KEY,
    ref_id      bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    -- endpoints are NAME-keyed text (block + port names), never block-row
    -- FKs — save_tree rebuilds every se_blocks.id on every save, so an
    -- id-keyed endpoint would strand immediately (nm persist's "Round-2
    -- landmine", designed out here from the start).
    a_block     text NOT NULL,
    a_port      text NOT NULL,
    b_block     text NOT NULL,
    b_port      text NOT NULL,
    -- the joint: kinematic class (rigid|revolute|prismatic|cylindrical|
    -- planar|ball|compliant|captive) + axis/allowed-DOF, and the optional
    -- separate mechanism (snap|screw|press|key|magnet|bearing|bond|
    -- integral) — se-kind.md's L2 two-axis split. Free jsonb until the
    -- joint ops land (slice 3 shapes it).
    joint       jsonb,
    -- objective vectors (loads etc.) — kind-neutral vocabulary, real units.
    objectives  jsonb,
    meta        jsonb,
    retired_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE se_connects IS
    'se port-to-port intent connections (docs/backlog/se-kind.md L0/L2): '
    'joint kinematic class x mechanism + objectives, name-keyed endpoints. '
    'Unused until the connect ops land.';

CREATE INDEX IF NOT EXISTS se_connects_ref_idx
    ON se_connects (ref_id);

COMMIT;

-- End of 0001_se_kind.sql

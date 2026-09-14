-- precis_se/0007_se_atomic.sql
--
-- The nm→se merge (docs/backlog/nm-se-merge.md): se's block tree grows the
-- **atomic mode**'s own storage — the columns and one table that
-- ``precis_nm``'s tables held for the kind now folding in. nm's migration
-- namespace is sealed history; its tables are dropped by a later migration
-- in this window, after the content move.
--
-- What lands here, and why each is a column on an existing table rather
-- than a new side table (the same reasoning ``nm_blocks.dof`` was given in
-- ``precis_nm/0001_nm_kind.sql``: one fact per block, no cardinality of its
-- own, so a join table would buy nothing):
--
-- * ``se_blocks.dof`` — the L2 declared degree of freedom
--   (``{'kind': 'rotational'|'translational', 'axis_ports': [p, q]}``),
--   stored explicitly and never re-derived from coordinates.
--
-- * ``se_ports.expected_element`` / ``expected_hybridization`` — the
--   scaffold-side chemistry a port demands of the atom it will one day
--   attach to; the gate ``bind_structure`` checks. Typed columns, not
--   ``annotations`` keys: they are checked capabilities, and the open
--   annotations dict is for what is merely descriptive
--   (blocktree-library-build-plan.md §Settled).
--
-- * ``se_ports.bound_design`` / ``bound_atom`` — the atom-side projection
--   of that one port fact (structure design slug + atom label within it).
--   Always both or neither.
--
-- * ``se_connects.kind`` — ``bond`` | ``interaction`` for an atomic-mode
--   edge; NULL for se's ordinary structural connect, whose L2 statement is
--   the ``joint`` column instead. NULL is therefore not "unknown" but "not
--   an atomic edge" — the capability gate
--   (:func:`precis_se.atomic.vocab.check_bond_capability`) reads exactly
--   ``kind = 'bond'``.
--
-- * ``se_topology`` — the L2 threading invariants (a macrocycle threaded on
--   an axle), directional and NAME-keyed from the start. ``nm_topology``
--   was born id-keyed (``subject_block``/``object_block`` FKs) and had to
--   be given name columns by ``0003_nm_bindings.sql`` because
--   ``persist.save_tree`` rebuilds every block row id on every save — this
--   table skips that detour, exactly as ``se_connects`` already did.
--
-- * ``se_blocks.bound_kind`` grows ``'structure'`` — an atomic-mode block's
--   L3 realization IS an atomistic design (``precis_se.modes``'s
--   ``atomic`` family declares ``realization_kinds=('structure','nm')``),
--   so ``set_binding``/``bind_structure`` need the kind the CHECK
--   constraint didn't have. ``'nm'`` stays legal here until the kind
--   itself retires.
--
-- Zero live rows in every se table in prod as of this migration.
--
-- Forward-only (ADR 0005). Idempotent. This is a PLUGIN migration
-- (namespace ``precis_se``), applied after 0001-0006.

BEGIN;

ALTER TABLE se_blocks ADD COLUMN IF NOT EXISTS dof jsonb;

COMMENT ON COLUMN se_blocks.dof IS
    'Atomic-mode L2 declared degree of freedom: {"kind": '
    '"rotational"|"translational", "axis_ports": [a, b]} — the two port '
    'names naming the axis, on this block''s OWN ports. Stored '
    'explicitly, never re-derived from L3 coordinates. An instance block '
    'resolves dof from its template at read time and always stores NULL.';

ALTER TABLE se_ports ADD COLUMN IF NOT EXISTS expected_element text;
ALTER TABLE se_ports ADD COLUMN IF NOT EXISTS expected_hybridization text;
ALTER TABLE se_ports ADD COLUMN IF NOT EXISTS bound_design text;
ALTER TABLE se_ports ADD COLUMN IF NOT EXISTS bound_atom text;

COMMENT ON COLUMN se_ports.expected_element IS
    'Atomic-mode: the element symbol this attachment point demands of the '
    'atom it binds to (the gate bind_structure checks). A checked '
    'capability, hence its own column rather than an annotations key.';
COMMENT ON COLUMN se_ports.bound_atom IS
    'Atom-side projection of this one port fact: the atom label within '
    'bound_design''s structure scene. Set together with bound_design by '
    'the store-aware bind_structure op, NULL until filled.';

ALTER TABLE se_connects ADD COLUMN IF NOT EXISTS kind text;

ALTER TABLE se_connects DROP CONSTRAINT IF EXISTS se_connects_kind_check;
ALTER TABLE se_connects ADD CONSTRAINT se_connects_kind_check
    CHECK (kind IS NULL OR kind IN ('bond', 'interaction')) NOT VALID;

COMMENT ON COLUMN se_connects.kind IS
    'bond | interaction for an atomic-mode edge; NULL for an ordinary se '
    'structural connect (whose L2 statement is the joint column). NULL '
    'means "not an atomic edge", not "unknown" — the bond capability gate '
    'fires on kind = ''bond'' exactly.';

CREATE TABLE IF NOT EXISTS se_topology (
    id            bigserial PRIMARY KEY,
    ref_id        bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    kind          text NOT NULL CHECK (kind IN ('threading')),
    -- NAME-keyed endpoints (block names), never block-row FKs — see this
    -- file's header: save_tree rebuilds every se_blocks.id on every save.
    subject_name  text NOT NULL,
    object_name   text NOT NULL,
    meta          jsonb,
    retired_at    timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE se_topology IS
    'Atomic-mode L2 topology invariants (nm-se-merge.md), one row per '
    'fact: kind=''threading'' means subject_name is threaded through '
    'object_name (a macrocycle on an axle) — directional, name-keyed, '
    'stored explicitly and never re-derived from geometry. Mutual '
    'threading is rejected at write time (each would be inside the '
    'other); a dangling name is a read-time DRC finding.';

CREATE UNIQUE INDEX IF NOT EXISTS se_topology_threading_pair_key
    ON se_topology (ref_id, subject_name, object_name)
    WHERE retired_at IS NULL AND kind = 'threading';

CREATE INDEX IF NOT EXISTS se_topology_ref_live_idx
    ON se_topology (ref_id) WHERE retired_at IS NULL;

ALTER TABLE se_blocks DROP CONSTRAINT IF EXISTS se_blocks_bound_kind_check;
ALTER TABLE se_blocks ADD CONSTRAINT se_blocks_bound_kind_check
    CHECK (bound_kind IN ('cad', 'nm', 'structure', 'component', 'part'))
    NOT VALID;

COMMIT;

-- End of 0007_se_atomic.sql

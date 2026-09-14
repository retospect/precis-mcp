-- 0161_pcb_generators.sql
--
-- pcb-ewod-multitile Slice 2: a `generators` block on `put(kind='pcb')`
-- names a computed-component generator call (name + generator type +
-- params) -- see `precis.pcb.generators.expand`. This table is the
-- IDENTITY/idempotency record only: the expansion itself (components,
-- pins, instances, nets, connections, footprints, features) lands as
-- ORDINARY rows in the existing pcb_* tables via `_pcb_apply`'s normal
-- insert path (the whole architecture's point -- nothing downstream needs
-- to know a component was generated rather than hand-authored). Re-running
-- `put` with the SAME `params` is a no-op (the row's `params` matches);
-- changed `params` retires the previous expansion's component/instance/
-- pins/nets/connections/features (via `refdes`/`net_prefix`) and re-runs
-- the generator, same discipline the card-variant synthesis pass uses for
-- ord<0 rows.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

CREATE TABLE IF NOT EXISTS pcb_generators (
    ref_id     bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    name       text   NOT NULL,
    generator  text   NOT NULL,
    version    integer NOT NULL DEFAULT 1,
    params     jsonb  NOT NULL,
    refdes     text   NOT NULL,
    ledger     jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (ref_id, name)
);

COMMENT ON TABLE pcb_generators IS
    'pcb-ewod-multitile Slice 2 -- one computed-component generator call '
    'per (ref_id, name): generator TYPE (e.g. ewod_pad_array) + fully-'
    'defaulted, canonical params + the ONE refdes it emits, for the '
    'idempotent no-op/retire-and-reinsert decision precis.store._pcb_ops '
    'makes on each put(generators=[...]). ledger is the last expansion''s '
    'capability summary (precis.pcb.generators.GeneratorExpansion.ledger), '
    'cached for read-back -- always re-derivable from params alone since '
    'expansion is a pure function.';

COMMIT;

-- End of 0161_pcb_generators.sql

-- 0218_pcb_roles.sql
--
-- docs/backlog/pcb-lazy-netlist-and-checks.md §1b / Slice 6: supply/client
-- roles. A part declares ``supplies`` (role name + pin blocks) on
-- pcb_components.meta, a terminal declares ``client_of`` on pcb_pins.meta,
-- and one pcb_roles row per (design, role) carries what is common to the
-- role: the working voltage and how many clients one supplier pin may
-- serve. Keyed by ref_id like pcb_net_classes (0138).
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

CREATE TABLE IF NOT EXISTS pcb_roles (
    role_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ref_id     bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    name       text   NOT NULL,             -- joins supplies.role / client_of.role
    voltage_v  double precision,            -- NULL = unspecified
    fanout     integer CHECK (fanout IS NULL OR fanout >= 1),  -- NULL = unbounded
    meta       jsonb  NOT NULL DEFAULT '{}',
    retired_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE pcb_roles IS
    'Per-design supply/client role (pcb-lazy-netlist-and-checks Slice 6): '
    'voltage_v and fanout (clients one supplier pin may serve; NULL = '
    'unbounded) for a role name that components declare in meta.supplies '
    'and pins declare in meta.client_of.';

CREATE UNIQUE INDEX IF NOT EXISTS pcb_roles_ref_name_key
    ON pcb_roles (ref_id, name) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS pcb_roles_ref_id_fk_idx
    ON pcb_roles (ref_id);

COMMIT;

-- End of 0218_pcb_roles.sql

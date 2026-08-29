-- 0141_pcb_pin_swaps.sql
--
-- docs/backlog/pcb-engine-plan.md's "PIN_SWAP is not persisted" finding:
-- ``optimize.py``'s PIN_SWAP move calls ``PcbIR.swap_pins`` (a genuine
-- pin<->net reassignment within one instance), but nothing wrote the
-- settled result back — so the stored netlist and the stored copper could
-- describe two different boards. This is a netlist edit, so it belongs
-- next to ``pcb_netconns`` (ADR 0042 §4), not folded into ``pcb_routes``'s
-- sketch shape.
--
-- **Not a rewrite of ``pcb_netconns``.** A physical pin is on AT MOST ONE
-- net there (``pcb_netconns_phys_pin_key``) and that row IS the netlist a
-- human authored — overwriting its ``net_id`` in place would destroy the
-- authored fact with no way to tell, later, that a search result (not a
-- human) put it there. ``pcb_pin_swaps`` is a **derived-assignment table**,
-- the exact shape ``pcb_planes`` already established for this problem
-- (gr267526): one row per physical pin whose EFFECTIVE net currently
-- differs from what ``pcb_netconns`` says, provenance-tagged via
-- ``meta.source`` ('authored' | 'derived'), authored rows never clobbered
-- by a derived replace. No caller currently writes an 'authored' row here
-- (there is no ``op='pin_swap'`` authoring verb yet — out of this
-- migration's scope, see the pcb_route/pcb_place job_type changes in the
-- same change); the column exists so that verb, when it lands, slots into
-- the same discipline the plane path already uses rather than inventing a
-- second one.
--
-- Same composite-FK shape as ``pcb_netconns`` (component_id denormalized
-- so the FKs force pin.component = instance.component).
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

CREATE TABLE IF NOT EXISTS pcb_pin_swaps (
    swap_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    board_id     bigint NOT NULL REFERENCES pcb_boards (board_id) ON DELETE CASCADE,
    instance_id  bigint NOT NULL,
    pin_id       bigint NOT NULL,
    component_id bigint NOT NULL,                  -- denormalized so the FKs force pin.component = instance.component
    net_id       bigint NOT NULL REFERENCES pcb_nets (net_id) ON DELETE CASCADE,
    meta         jsonb  NOT NULL DEFAULT '{}',      -- {"source": "authored"|"derived"}
    retired_at   timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (instance_id, component_id)
        REFERENCES pcb_instances (instance_id, component_id) ON DELETE CASCADE,
    FOREIGN KEY (pin_id, component_id)
        REFERENCES pcb_pins (pin_id, component_id) ON DELETE CASCADE
);

COMMENT ON TABLE pcb_pin_swaps IS
    'DERIVED pin<->net override (pcb-engine-plan "PIN_SWAP is not '
    'persisted") — one row per physical pin whose effective net differs '
    'from pcb_netconns, gr267526''s provenance discipline reused: '
    'meta.source authored|derived, a derived replace never touches an '
    'authored row. pcb_netconns itself is never rewritten by a swap.';

-- a physical pin has AT MOST ONE live swap-override, same shape as
-- pcb_netconns_phys_pin_key
CREATE UNIQUE INDEX IF NOT EXISTS pcb_pin_swaps_phys_pin_key
    ON pcb_pin_swaps (instance_id, pin_id) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS pcb_pin_swaps_board_idx
    ON pcb_pin_swaps (board_id) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS pcb_pin_swaps_board_id_fk_idx
    ON pcb_pin_swaps (board_id);
-- net_id isn't a leading prefix of the (instance_id, pin_id) partial
-- unique index -- its own FK-cascade coverage.
CREATE INDEX IF NOT EXISTS pcb_pin_swaps_net_id_fk_idx
    ON pcb_pin_swaps (net_id);
-- the composite FKs' own covering indexes (0136_fk_covering_indexes'
-- precedent for pcb_netconns' identical shape: `phys_pin_key` above is
-- PARTIAL and in (instance_id, pin_id) order, so it covers neither
-- 2-column FK below).
CREATE INDEX IF NOT EXISTS pcb_pin_swaps_instance_component_idx
    ON pcb_pin_swaps (instance_id, component_id);
CREATE INDEX IF NOT EXISTS pcb_pin_swaps_pin_component_idx
    ON pcb_pin_swaps (pin_id, component_id);

COMMIT;

-- End of 0141_pcb_pin_swaps.sql

-- 0221_pcb_netlist_snapshots.sql
--
-- docs/backlog/pcb-lazy-netlist-and-checks.md §1d / Slice 7: the resolved
-- netlist snapshot replaces pcb_pin_swaps. pcb_pin_swaps was back-
-- annotation implemented internally: a per-pin override replayed onto
-- every freshly built IR. A snapshot is the value instead: the FULL
-- effective (pin, net) set one resolve produced (Reto 2026-10-16 ruling
-- 1a: "one place to see all the things"), the prior assignment it was
-- solved from, and the content_hash of the intent it was solved for.
-- Derived and regenerable like pcb_copper: a resolve appends a row,
-- nothing edits one; readers take the newest per board.
--
-- pins: [{refdes, pin, net, declared, solver_created?}] sorted by
-- (refdes, pin). `declared` is the pin's authored net at solve time; a
-- reader applies an entry only while that still holds. `solver_created`
-- marks a pin with no declared net that the solve bound (a spare supply
-- channel taking a client net, ruling 2a); it is never written back to
-- pcb_netconns. Keyed by names, not ids, like the pin_swaps reader was.
-- prior: [[client_refdes, client_pin, supply_refdes, supply_pin], ...].
--
-- Data: every board with live pcb_pin_swaps rows (all meta.source =
-- 'derived' on prod at authoring time) gets one snapshot built from
-- pcb_netconns with those overrides applied, content_hash NULL and
-- meta.source = 'pcb_pin_swaps'; then pcb_pin_swaps is dropped. Nothing
-- is lost (Reto 2026-10-16: "Carry them verbatim"); meta also carries:
--   swap_rows    the board's live swap count;
--   unmapped     verbatim live rows naming a retired instance/pin/net
--                (160 of 269 on prod at authoring time; never applied);
--   retired_rows / retired  every retired row verbatim (900 on prod).
-- The drop is refused unless (a) swap_rows + retired_rows over all
-- migrated snapshots equals count(*) of the whole table, and (b) per
-- board, every live row is either in unmapped or appears in pins as
-- (refdes, pin, net). The store never prunes these snapshots.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

CREATE TABLE IF NOT EXISTS pcb_netlist_snapshots (
    snapshot_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    board_id     bigint NOT NULL REFERENCES pcb_boards (board_id) ON DELETE CASCADE,
    content_hash text,                                -- NULL = migrated from pcb_pin_swaps
    pins         jsonb  NOT NULL DEFAULT '[]',
    prior        jsonb  NOT NULL DEFAULT '[]',
    meta         jsonb  NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE pcb_netlist_snapshots IS
    'DERIVED resolved netlist (pcb-lazy-netlist-and-checks Slice 7): the '
    'full effective (pin, net) set one resolve produced, the prior it was '
    'solved from and the intent content_hash. Appended per resolve, never '
    'edited; readers take the newest per board. Replaces pcb_pin_swaps.';

CREATE INDEX IF NOT EXISTS pcb_netlist_snapshots_board_idx
    ON pcb_netlist_snapshots (board_id, snapshot_id DESC);

DO $$
BEGIN
    IF to_regclass('pcb_pin_swaps') IS NOT NULL THEN
        INSERT INTO pcb_netlist_snapshots (board_id, content_hash, pins, meta)
        SELECT b.board_id, NULL,
            COALESCE((
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'refdes', e.refdes, 'pin', e.pin,
                        'net', e.net, 'declared', e.declared
                    )
                    || CASE WHEN e.declared IS NULL AND e.net IS NOT NULL
                            THEN '{"solver_created": true}'::jsonb
                            ELSE '{}'::jsonb END
                    ORDER BY e.refdes, e.pin)
                FROM (
                    SELECT i.refdes, p.name AS pin,
                           COALESCE(sn.name, dn.name) AS net,
                           dn.name AS declared
                    FROM (
                        SELECT nc.instance_id, nc.pin_id
                        FROM pcb_netconns nc
                        JOIN pcb_nets n ON n.net_id = nc.net_id
                        WHERE n.ref_id = b.ref_id AND n.retired_at IS NULL
                        UNION
                        SELECT sw.instance_id, sw.pin_id
                        FROM pcb_pin_swaps sw
                        WHERE sw.board_id = b.board_id AND sw.retired_at IS NULL
                    ) k
                    JOIN pcb_instances i ON i.instance_id = k.instance_id
                    JOIN pcb_pins p ON p.pin_id = k.pin_id
                    LEFT JOIN (pcb_netconns nc
                               JOIN pcb_nets dn
                                 ON dn.net_id = nc.net_id AND dn.retired_at IS NULL)
                        ON nc.instance_id = k.instance_id AND nc.pin_id = k.pin_id
                    LEFT JOIN (pcb_pin_swaps sw
                               JOIN pcb_nets sn
                                 ON sn.net_id = sw.net_id AND sn.retired_at IS NULL)
                        ON sw.instance_id = k.instance_id AND sw.pin_id = k.pin_id
                       AND sw.retired_at IS NULL
                    WHERE i.retired_at IS NULL AND p.retired_at IS NULL
                ) e
            ), '[]'::jsonb),
            jsonb_build_object(
                'source', 'pcb_pin_swaps',
                'swap_rows', (
                    SELECT count(*) FROM pcb_pin_swaps sw
                    WHERE sw.board_id = b.board_id AND sw.retired_at IS NULL
                ),
                -- live swap rows that name a retired instance, pin or net
                -- (the design was re-put since): carried verbatim, never
                -- applied, so the drop below loses no row silently.
                'unmapped', COALESCE((
                    SELECT jsonb_agg(jsonb_build_object(
                        'swap_id', sw.swap_id,
                        'instance_id', sw.instance_id, 'refdes', i.refdes,
                        'pin_id', sw.pin_id, 'pin', p.name,
                        'net_id', sw.net_id, 'net', n.name,
                        'meta', sw.meta, 'created_at', sw.created_at
                    ) ORDER BY sw.swap_id)
                    FROM pcb_pin_swaps sw
                    LEFT JOIN pcb_instances i ON i.instance_id = sw.instance_id
                    LEFT JOIN pcb_pins p ON p.pin_id = sw.pin_id
                    LEFT JOIN pcb_nets n ON n.net_id = sw.net_id
                    WHERE sw.board_id = b.board_id AND sw.retired_at IS NULL
                      AND (i.retired_at IS NOT NULL OR p.retired_at IS NOT NULL
                           OR n.retired_at IS NOT NULL OR i.instance_id IS NULL
                           OR p.pin_id IS NULL OR n.net_id IS NULL)
                ), '[]'::jsonb),
                'retired_rows', (
                    SELECT count(*) FROM pcb_pin_swaps sw
                    WHERE sw.board_id = b.board_id AND sw.retired_at IS NOT NULL
                ),
                'retired', COALESCE((
                    SELECT jsonb_agg(jsonb_build_object(
                        'swap_id', sw.swap_id,
                        'instance_id', sw.instance_id, 'refdes', i.refdes,
                        'pin_id', sw.pin_id, 'pin', p.name,
                        'net_id', sw.net_id, 'net', n.name,
                        'meta', sw.meta, 'created_at', sw.created_at,
                        'retired_at', sw.retired_at
                    ) ORDER BY sw.swap_id)
                    FROM pcb_pin_swaps sw
                    LEFT JOIN pcb_instances i ON i.instance_id = sw.instance_id
                    LEFT JOIN pcb_pins p ON p.pin_id = sw.pin_id
                    LEFT JOIN pcb_nets n ON n.net_id = sw.net_id
                    WHERE sw.board_id = b.board_id AND sw.retired_at IS NOT NULL
                ), '[]'::jsonb)
            )
        FROM pcb_boards b
        WHERE EXISTS (
            SELECT 1 FROM pcb_pin_swaps sw WHERE sw.board_id = b.board_id
        );
        -- Refuse rather than drop: (a) every row, live or retired, is
        -- carried by exactly one migrated snapshot ...
        IF (SELECT count(*) FROM pcb_pin_swaps)
           <> (SELECT COALESCE(sum((meta->>'swap_rows')::bigint
                                   + (meta->>'retired_rows')::bigint), 0)
               FROM pcb_netlist_snapshots WHERE meta->>'source' = 'pcb_pin_swaps')
           OR (SELECT count(*) FROM pcb_pin_swaps)
           <> (SELECT COALESCE(sum(jsonb_array_length(meta->'retired'))
                               + sum((meta->>'swap_rows')::bigint), 0)
               FROM pcb_netlist_snapshots WHERE meta->>'source' = 'pcb_pin_swaps')
        THEN
            RAISE EXCEPTION '0221: pcb_pin_swaps rows do not match the migrated snapshots';
        END IF;
        -- ... and (b) per board every live row is either carried verbatim
        -- in unmapped or applied in pins as (refdes, pin, net).
        IF EXISTS (
            SELECT 1 FROM pcb_netlist_snapshots s
            WHERE s.meta->>'source' = 'pcb_pin_swaps'
              AND jsonb_array_length(s.meta->'unmapped') + (
                    SELECT count(*) FROM pcb_pin_swaps sw
                    JOIN pcb_instances i ON i.instance_id = sw.instance_id
                    JOIN pcb_pins p ON p.pin_id = sw.pin_id
                    JOIN pcb_nets n ON n.net_id = sw.net_id
                    WHERE sw.board_id = s.board_id AND sw.retired_at IS NULL
                      AND i.retired_at IS NULL AND p.retired_at IS NULL
                      AND n.retired_at IS NULL
                      AND s.pins @> jsonb_build_array(jsonb_build_object(
                            'refdes', i.refdes, 'pin', p.name, 'net', n.name))
                  ) <> (s.meta->>'swap_rows')::bigint
        ) THEN
            RAISE EXCEPTION '0221: a live pcb_pin_swaps row is neither applied nor carried';
        END IF;
        -- The drop takes ACCESS EXCLUSIVE on every table pcb_pin_swaps
        -- references; fail fast rather than queue pcb reads behind a
        -- long-running one (as 0185 does).
        SET LOCAL lock_timeout = '5s';
        DROP TABLE pcb_pin_swaps;
    END IF;
END
$$;

COMMIT;

-- End of 0221_pcb_netlist_snapshots.sql

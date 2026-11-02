-- 0273_machine_kind.sql
--
-- Register `kind='machine'` — one ref per host in the fleet (gr549585,
-- Reto ruling 2026-11-02: "new machine kind as recommended").
--
-- get(kind='machine', id='ma_<host>') reads a slug-addressed ref whose
-- body holds stable facts only (platform, role, GPU) and overlays live
-- state — load average, CPU temperature, last-seen — read at request
-- time from ``host_heartbeat`` (migration 0017), never stored in the
-- ref body, chunks or embeddings. A host with no heartbeat row shows
-- 'no heartbeat', never 'down'. search(kind='machine') filters by
-- platform, role and status (derived from heartbeat age: live / stale /
-- no heartbeat). Refs are created/refreshed by an operator-run
-- put(kind='machine', mode='sync') — no cadence wires it.
--
-- Data-only: the kind reuses refs + chunks + ref_identifiers (the
-- cite_key slug row every slug-addressed kind takes). One registry
-- row:
--   * `kinds.machine` — runtime kind validator
--
-- Forward-only (ADR 0005). Idempotent under ON CONFLICT DO NOTHING.

BEGIN;

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('machine', FALSE, 'Machine',
     'A host in the fleet — one ref per machine, handle ma_<host>. Body '
     'holds stable facts only (platform, role, GPU); live load, CPU '
     'temperature and last-seen are overlaid at request time from '
     'host_heartbeat (a host with no row shows ''no heartbeat'', never '
     '''down''). search filters by platform, role and status '
     '(live / stale / no heartbeat). Refs come from '
     'put(kind=''machine'', mode=''sync''), run by hand. See '
     '``precis-machine-help``.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0273_machine_kind.sql

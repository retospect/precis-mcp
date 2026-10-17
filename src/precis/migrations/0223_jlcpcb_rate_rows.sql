-- 0223_jlcpcb_rate_rows.sql
--
-- docs/backlog/shared-rate-limiter.md: fleet-wide pacing for the two JLC
-- services (Reto 2026-10-16, "yes"). Until now the 0.5 s BULK_POLICY spacing
-- in precis.pcb._http was per process, so N cluster hosts each walked at 2/s
-- and JLC saw N times that. Two rows, one per upstream:
--
--   jlcpcb      the Open API (jlc_api, jlc_order: stock/price, orders)
--   jlcpcb-web  the parts-library reader (jlc_library: web catalog walk)
--
-- Each is a one-token bucket refilling at 1/s: calls never closer than 1 s
-- apart (so the 0.5 s floor holds with room), no burst, and the two rows
-- together stay at or under the agreed 2 req/s ceiling. No daily cap.
-- Upsert so a re-run, or a database that already has a hand-made row, ends
-- with the same values.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

INSERT INTO external_rate_limits (provider, capacity, refill_per_sec, tokens, daily_cap)
VALUES ('jlcpcb', 1, 1.0, 1, NULL),
       ('jlcpcb-web', 1, 1.0, 1, NULL)
ON CONFLICT (provider) DO UPDATE
   SET capacity       = EXCLUDED.capacity,
       refill_per_sec = EXCLUDED.refill_per_sec,
       daily_cap      = EXCLUDED.daily_cap,
       tokens         = LEAST(external_rate_limits.tokens, EXCLUDED.capacity);

COMMIT;

-- End of 0223_jlcpcb_rate_rows.sql

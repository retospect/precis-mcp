-- 0222_crossref_rate_1rps.sql
--
-- docs/backlog/shared-rate-limiter.md, ruling 1 (Reto 2026-10-16: "Be nice.
-- We have time."): Crossref gets at most 1 request/s cluster-wide, whatever
-- its headers advertise (2026-10-16 reading: x-rate-limit-limit 3 per 1s,
-- polite pool). 0121 seeded the crossref row at 20 rps; lower it to a
-- one-token bucket refilling at 1/s. Upsert so a database without the 0121
-- seed row still ends with the 1/s row.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

INSERT INTO external_rate_limits (provider, capacity, refill_per_sec, tokens, daily_cap)
VALUES ('crossref', 1, 1.0, 1, NULL)
ON CONFLICT (provider) DO UPDATE
   SET capacity       = EXCLUDED.capacity,
       refill_per_sec = EXCLUDED.refill_per_sec,
       tokens         = LEAST(external_rate_limits.tokens, EXCLUDED.capacity);

COMMIT;

-- End of 0222_crossref_rate_1rps.sql

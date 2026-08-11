-- 0121_external_rate_limits.sql
--
-- General, DB-backed, cross-host rate limiter for outbound external APIs.
-- Today ~5 worker hosts each run `_SYS` passes that hit Semantic Scholar
-- concurrently with only per-thread tenacity backoff — no cross-host
-- coordination, so the cluster collectively angers S2 (~1 rps ceiling).
--
-- `external_rate_limits` is one row per provider; the atomic UPDATE against
-- a single row (`precis.utils.rate_limit.acquire`) is the cross-host
-- coordination point — analogous to `resource_slots` for LLM backends, but
-- rate/quota-shaped for external HTTP instead of concurrency-shaped for LLM
-- serving. Two independent lanes per row: a token-bucket rate lane
-- (capacity/refill_per_sec/tokens/last_refill) and an optional daily-quota
-- lane (daily_cap/day_used/day_start; `daily_cap IS NULL` means inert).
--
-- v1 wires only `s2` (rate lane, no daily cap). `openalex`/`unpaywall`
-- carry `daily_cap` for their dormant quota lane (100k/day hard caps);
-- `arxiv`/`crossref` are pre-seeded config-only. None of these four are
-- wired to a caller yet — seeding them now means future wiring needs no
-- schema migration, just an `acquire(provider)` call site.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

CREATE TABLE IF NOT EXISTS external_rate_limits (
    provider        text PRIMARY KEY,
    capacity        integer NOT NULL,          -- token-bucket burst size
    refill_per_sec  numeric NOT NULL,          -- token refill rate (tokens/sec)
    tokens          numeric NOT NULL,          -- current tokens
    last_refill     timestamptz NOT NULL DEFAULT now(),
    daily_cap       integer,                   -- NULL = no daily quota lane
    day_used        integer NOT NULL DEFAULT 0,
    day_start       date NOT NULL DEFAULT CURRENT_DATE
);

-- Seed: s2 is wired in v1 (rate lane, no daily cap). openalex/unpaywall carry
-- daily_cap for the dormant quota lane. arxiv/crossref pre-seeded config-only.
INSERT INTO external_rate_limits (provider, capacity, refill_per_sec, tokens, daily_cap) VALUES
    ('s2',        2,  1.0,  2, NULL),
    ('openalex', 10,  8.0, 10, 100000),
    ('unpaywall', 5,  5.0,  5, 100000),
    ('arxiv',     1,  0.34, 1, NULL),
    ('crossref', 20, 20.0, 20, NULL)
ON CONFLICT (provider) DO NOTHING;

COMMIT;

-- End of 0121_external_rate_limits.sql

-- 0225_outbound_limiter_rows.sql
--
-- gr493222 (outbound politeness, Reto 2026-10-16 ~21:30Z): one shared
-- limiter row per third-party service, so precis.utils.http.with_backoff /
-- call_with_backoff pace every host from one bucket. Before this only s2,
-- openalex, unpaywall, arxiv and crossref had rows (0121). Rates sit at or
-- under each service's published limit; daily caps are the vendors' default
-- API-key quotas (raise the row if the account tier is higher).
--
-- crossref: Crossref's polite pool allows 10/s for single DOIs. Lowered to
-- at most 10/s here, never raised: a stricter row (the pending 1/s ruling,
-- "Be nice. We have time.") wins whichever migration runs last.
-- crossref-list: list/search queries, Crossref asks 3/s; never faster than
-- the crossref row.
--
-- opsin / cir: the keys handlers/_chem_resolve.py reads (gr493217,
-- service.lower() of "OPSIN"/"CIR"); one request a second, no burst, to
-- match its >=1 s spacing with one call in flight.
--
-- New rows are DO NOTHING, so a row another migration (jlcpcb, 0223) or an
-- operator already set keeps its values. Number follows landing order;
-- renumber at /go if it moves.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

UPDATE external_rate_limits
   SET capacity       = LEAST(capacity, 10),
       refill_per_sec = LEAST(refill_per_sec, 10.0),
       tokens         = LEAST(tokens, 10)
 WHERE provider = 'crossref';

INSERT INTO external_rate_limits (provider, capacity, refill_per_sec, tokens, daily_cap)
SELECT 'crossref-list',
       LEAST(3, COALESCE(MIN(capacity), 3)),
       LEAST(3.0, COALESCE(MIN(refill_per_sec), 3.0)),
       LEAST(3, COALESCE(MIN(capacity), 3)),
       NULL
  FROM external_rate_limits WHERE provider = 'crossref'
ON CONFLICT (provider) DO NOTHING;

INSERT INTO external_rate_limits (provider, capacity, refill_per_sec, tokens, daily_cap) VALUES
    ('openalex-content', 2, 2.0,  2, NULL),
    ('europepmc',        5, 5.0,  5, NULL),
    ('core',             1, 0.5,  1, NULL),
    ('datacite',         5, 5.0,  5, NULL),
    ('orcid',            8, 8.0,  8, NULL),
    ('pubchem',          5, 5.0,  5, NULL),
    ('opsin',            1, 1.0,  1, NULL),
    ('cir',              1, 1.0,  1, NULL),
    ('sec',              5, 5.0,  5, NULL),
    ('google-patents',   1, 0.5,  1, NULL),
    ('nanopub-registry', 2, 2.0,  2, NULL),
    ('materialsproject', 5, 5.0,  5, NULL),
    ('easyeda',          1, 0.5,  1, NULL),
    ('jlcpcb',           1, 1.0,  1, NULL),
    ('digikey',          2, 2.0,  2, 1000),
    ('mouser',           1, 0.5,  1, 1000),
    ('farnell',          2, 2.0,  2, 1000)
ON CONFLICT (provider) DO NOTHING;

COMMIT;

-- End of 0225_outbound_limiter_rows.sql

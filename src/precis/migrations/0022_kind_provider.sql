-- 0022_kind_provider — per-process registry of which (host, process)
-- has which kinds enabled.
--
-- Companion to the boot-time ``kinds`` upsert (see
-- ``store/_kinds_ops.py``). The ``kinds`` table records the cluster-
-- wide union of every-kind-anyone-has-ever-registered (FK target for
-- ``refs.kind``); this finer-grained table records *which processes*
-- can actually serve a kind right now, so when a process is asked
-- for a kind it doesn't have, the error can name the host(s) that do.
--
-- Use case: a chatter B without ``PERPLEXITY_API_KEY`` set in env
-- registers everything except ``perplexity-research`` /
-- ``perplexity-reasoning`` in its hub; if an agent asks B to
-- ``get(kind='perplexity-research', …)``, B's runtime can lift the
-- "available on hosts: melchior" hint from this table.
--
-- Stale entries are tolerated: the validator filters on
-- ``last_seen > now() - interval '1 hour'`` so a host that crashed
-- yesterday isn't proposed as a route. Boot-time upserts happen on
-- every process restart, so even ~minute-scale restart cadence keeps
-- the snapshot current.

CREATE TABLE kind_provider (
    slug TEXT NOT NULL,
    host TEXT NOT NULL,
    process TEXT NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (slug, host, process)
);

-- Index for the validator's "which hosts can serve this kind?" query.
-- Slug-led so the lookup is a single index scan.
CREATE INDEX kind_provider_slug_recent_idx
    ON kind_provider (slug, last_seen DESC);

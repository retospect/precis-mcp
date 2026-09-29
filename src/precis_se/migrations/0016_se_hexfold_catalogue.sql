-- precis_se/0016_se_hexfold_catalogue.sql
--
-- `se_hexfold_catalogue` — the DB-backed store behind
-- :class:`hexfold.catalogue.CatalogueStore`
-- (docs/backlog/hexfold-integration.md step 6 slice 2; the in-process
-- twin is `hexfold.catalogue.MemoryStore`, which names this table's
-- module as its persistent counterpart).
--
-- Rows are keyed by ENVIRONMENT, not by instance: a rim type at a given
-- dangling count on a given relax rung decays the same way wherever it
-- occurs, so `hexfold.join.compose`'s seam radius and leak threshold can
-- be looked up instead of baked into module constants. The key is
-- `hexfold.catalogue.EnvKey` — zone/lattice/sigma/kind/nm/rim_type/N/
-- seam/rung/relaxer — and `key_hash` is the sha256 of its
-- `canonical_json()`, computed in Python so the two stores agree by
-- construction.
--
-- NOT ref-scoped, deliberately, and this is the one thing to understand
-- before adding a column: there is no `ref_id`. A measured decay length
-- is a fact about sp2 carbon at a rim, not about the design that
-- happened to ask first, so one warm row serves every design in the
-- database. That also means a bad row is a SHARED bad row — see the
-- `source` column's comment.
--
-- This is NOT SPEC §26's `hexfold_cache`. That table is a content-hash
-- BUILD cache (authored_json -> generated_json, keyed by the §14 hash of
-- the authored sections plus generator version) and does not exist yet;
-- this one is the environment-keyed catalogue of §25.3 `view='catalogue'`
-- and the step 6 ruling. The spec runs the two together under one
-- heading; erratum filed in docs/backlog/hexfold-integration.md. Do not
-- widen either into the other.
--
-- Zero live rows in prod as of this migration (the table is new).
--
-- The migration bodies get `BEGIN;`/`COMMIT;` stripped and executed
-- whole by the plugin-migration test fixtures (0009's header), so every
-- statement below stays independently runnable.
--
-- Forward-only (ADR 0005). Idempotent. This is a PLUGIN migration
-- (namespace `precis_se`), applied after 0001-0015.

BEGIN;

-- 1. the environment-keyed catalogue ---------------------------------------
CREATE TABLE IF NOT EXISTS se_hexfold_catalogue (
    key_hash        text PRIMARY KEY,
    zone            text NOT NULL,
    key_json        jsonb NOT NULL,
    row_json        jsonb NOT NULL,
    source          text NOT NULL,
    hexfold_version text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE se_hexfold_catalogue IS
    'Environment-keyed seam/bulk geometry rows for hexfold '
    '(precis_se.atomic.catalogue, the DB CatalogueStore). One row per '
    'hexfold.catalogue.EnvKey; no ref_id, because the row describes a '
    'rim type at a dangling count on a relax rung, not a design. '
    'hexfold.join.compose is READ-ONLY on this table — rows are written '
    'only by an explicit hexfold.catalogue.measure_environment warm-up '
    'or a forced load. NOT SPEC §26''s hexfold_cache, which is a '
    'content-hash build cache and a different table.';

COMMENT ON COLUMN se_hexfold_catalogue.key_hash IS
    'sha256 of hexfold.catalogue.EnvKey.canonical_json(), computed in '
    'Python (EnvKey.hash()) — never in SQL, so MemoryStore and this '
    'store cannot drift apart on key construction.';

COMMENT ON COLUMN se_hexfold_catalogue.zone IS
    'EnvKey.zone: ''bulk'' | ''edge'' | ''seam''. Denormalised out of '
    'key_json so CatalogueStore.rows(zone) is an index scan, and it '
    'doubles as the row-type discriminator — bulk->BulkCell, '
    'edge->EdgeMotif, seam->SeamMotif is total and one-to-one, so there '
    'is no second type column to keep in step.';

COMMENT ON COLUMN se_hexfold_catalogue.key_json IS
    'The full EnvKey as written by hexfold.catalogue._key_to_dict — the '
    'preimage of key_hash, kept so a key can be audited and so edge '
    'lookups can filter on rim_type/N/rung without rehydrating rows.';

COMMENT ON COLUMN se_hexfold_catalogue.row_json IS
    'The row''s own to_dict() payload (BulkCell/EdgeMotif/SeamMotif), '
    'round-tripped by the matching from_dict. It repeats key_json under '
    'its "key" member by design: the dataclasses own their own '
    'serialisation and this table stores it verbatim rather than '
    'reassembling it.';

COMMENT ON COLUMN se_hexfold_catalogue.source IS
    'Provenance, copied from the row: ''pinned-<date>'' (a seed_rows '
    'wildcard restating hexfold.join''s constants), ''measured'' '
    '(measure_environment), ''forced'', ''join''. This is a gate, not a '
    'label: as of step 6 slice 2 a ''measured'' row is stored and '
    'reported but NEVER preferred over the pinned wildcard, because '
    'measure_environment''s seam radius tracks the length of the '
    'measurement tube rather than the EnvKey (gripe 456641). Since the '
    'table is shared across every design, one warm-up at an unlucky '
    'tube length would otherwise set the guard band fleet-wide.';

-- `rows(zone)` is the only scan the store does; `measured` rows are
-- partitioned off by source in the same predicate often enough to earn
-- the composite.
CREATE INDEX IF NOT EXISTS se_hexfold_catalogue_zone_source_idx
    ON se_hexfold_catalogue (zone, source);

-- Edge resolution (rim_type, N, rung, sigma) is the hot lookup: it runs
-- once per side per join, so it gets its own expression index rather
-- than filtering every edge row in Python.
CREATE INDEX IF NOT EXISTS se_hexfold_catalogue_edge_idx
    ON se_hexfold_catalogue (
        (key_json->>'rim_type'),
        (key_json->>'rung'),
        (key_json->>'sigma')
    )
    WHERE zone = 'edge';

COMMIT;

-- End of 0016_se_hexfold_catalogue.sql

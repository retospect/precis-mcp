-- 0337_ref_identifiers_cite_key_trgm_widen.sql
--
-- gr732478: in a ~2.9-day production window, ref_identifiers recorded
-- 5,492 sequential scans averaging ~204k rows, and the cite-key trigram
-- index recorded zero scans. These aggregate counters do not attribute
-- the sequential scans to particular queries.
--
-- Six prefix probes filter id_kind IN ('cite_key', 'cite_key_alias'):
-- ingest/db_writer.py::resolve_cite_key and ::resolve_slug,
-- ingest/provenance.py::_next_notice_seq, the stub fold in workers/chase.py,
-- store/_identifiers_ops.py::suggest_cite_key, and the stub mint in
-- store/_refs_ops.py. The old index predicate (id_kind = 'cite_key')
-- excludes aliases, so it cannot serve these probes. Widening the
-- predicate makes the index eligible without changing lookup semantics.
-- precis_pathway/handler.py also searches cite_key identifiers with
-- ILIKE. Its cite_key-only filter remains within the widened predicate.
--
-- In the prior build's PG 18 scratch test (~376k synthetic identifiers),
-- the mint probe switched from a parallel sequential scan to a bitmap
-- index scan. Literal and generic plans and the slug equality-or-prefix
-- form were exercised there. Production plan choice still depends on
-- query patterns, data distribution and planner costs.
--
-- Rebuild in one transaction because the migration runner wraps each
-- file in a transaction. The plain CREATE INDEX blocks writes during
-- the build.
--
DROP INDEX IF EXISTS public.ref_identifiers_cite_key_trgm_idx;

CREATE INDEX IF NOT EXISTS ref_identifiers_cite_key_trgm_idx ON public.ref_identifiers
    USING gin (id_value public.gin_trgm_ops)
    WHERE (id_kind IN ('cite_key'::text, 'cite_key_alias'::text));

-- End of 0337_ref_identifiers_cite_key_trgm_widen.sql

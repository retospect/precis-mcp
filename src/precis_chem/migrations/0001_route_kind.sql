-- precis_chem/0001_route_kind.sql
--
-- The `route` kind — a retrosynthesis route-graph IR (ADR 0056; design-of-record
-- docs/design/chem-tools-integration.md §2). A slug-addressed authored artifact
-- (like structure/cad/pcb): `title` = a human name, `meta.route` carries the
-- normalized route graph (target SMILES + ordered steps + per-node stock status
-- + confidence + provenance) written back by the `retrosynth` compute job. The
-- LLM-readable rendering is emitted as the reused `card_combined` chunk (ord=-1,
-- no new chunk_kind) so a route IS a vector in the corpus manifold.
--
-- corpus_role is authored/'none' (a synthetic route is never cited as evidence)
-- — enforced by the handler, not here. The kind ships DARK behind
-- PRECIS_CHEM_ENABLED (the handler's KindSpec.requires_env); seeding the kind row
-- is inert until the flag turns the kind on.
--
-- Also registers the route-step link relation. Per gripe 160213 the read-time
-- inverse rewrite is still Python-dict-bound, so slice-1 plugin relations must be
-- SYMMETRIC — `route-step` is symmetric (a precursor/product edge you can walk
-- from either end without the inverse map). The precise consumes/produces
-- direction lives in meta.route's step graph, not the link.
--
-- Forward-only (ADR 0005). Idempotent. This is a PLUGIN migration (namespace
-- `precis_chem`), applied after core via Migrator.discover_sources — so the
-- kinds/relations reference tables + the 0023 plugin column already exist.

BEGIN;

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('route', FALSE, 'Route',
     'A retrosynthesis route-graph (precis-chem plugin, ADR 0056): a target '
     'molecule + an ordered graph of reaction steps (precursors, template, '
     'conditions, stock status, confidence) planned by a swappable engine '
     '(AiZynthFinder / ASKCOS / …) on the compute lane. The LLM traverses the '
     'graph, never runs a planner in the request path. See '
     'docs/design/chem-tools-integration.md.')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('route-step', TRUE, NULL,
     'Symmetric edge between a route and a precursor/intermediate artifact it '
     'consumes or produces. Direction (consumes vs produces) lives in the '
     'route graph in meta.route, not the link (gripe 160213: plugin relations '
     'stay symmetric until the read-time inverse map is DB-sourced).')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

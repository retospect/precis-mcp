-- precis_bio/0001_protein_kind.sql
--
-- The `protein` kind — a predicted-protein-structure IR (ADR 0056 slice 4;
-- design-of-record docs/design/chem-tools-integration.md). A slug-addressed
-- authored artifact (like structure/route): `title` = a human name,
-- `meta.fold` carries the normalized fold (the mmCIF structure + mean pLDDT +
-- pTM/ipTM + sequence + provenance) written back by the `fold` compute job.
-- The LLM-readable rendering is emitted as the reused `card_combined` chunk
-- (ord=-1, no new chunk_kind) so a protein IS a vector in the corpus manifold
-- (searchable by sequence/name).
--
-- corpus_role is authored/'none' (a synthetic fold is never cited as evidence)
-- — enforced by the handler, not here. The kind ships DARK behind
-- PRECIS_BIO_ENABLED (the handler's KindSpec.requires_env); seeding the kind
-- row is inert until the flag turns the kind on.
--
-- No plugin relation is registered: the `fold` job blocks a requesting todo
-- through the CORE `requested` relation (ADR 0044), which already exists.
--
-- Forward-only (ADR 0005). Idempotent. This is a PLUGIN migration (namespace
-- `precis_bio`), applied after core via Migrator.discover_sources — so the
-- kinds reference table + the 0023 plugin column already exist.

BEGIN;

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('protein', FALSE, 'Protein',
     'A predicted protein structure (precis-bio plugin, ADR 0056): an '
     'amino-acid sequence + its folded structure (mmCIF, mean pLDDT, pTM/ipTM) '
     'predicted by a swappable engine (AlphaFold3 de-novo / ColabFold MSA) on '
     'the compute lane. The LLM reads confidences + sequence, never runs a GPU '
     'fold in the request path. See docs/design/chem-tools-integration.md.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

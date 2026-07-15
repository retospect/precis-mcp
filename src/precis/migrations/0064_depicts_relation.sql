-- 0064_depicts_relation.sql
--
-- The `depicts` / `depicted-in` chunk-level relation (ADR 0057).
--
-- A diagram element (an SVG shape or a mermaid node, identified by its
-- stable source `id=`) binds to the chunk it depicts — a draft `dc…`
-- chunk, a paper chunk, a memory record. The binding is a chunk→chunk
-- (or chunk→ref) `depicts` link from the diagram's SOURCE chunk
-- (`figure_node` / `mermaid_node`) to the target. The element id(s) live
-- in `links.meta.elements` (a set), NOT in a column: the links UNIQUE key
-- is (src_ref, src_chunk, dst_ref, dst_chunk, relation), so ONE row per
-- edge carries every element that anchors it. This is the element-granular
-- cousin of 0035's whole-figure `plots` / `plotted-by`.
--
-- Additive: no new tables or columns — `links.src_chunk_id` /
-- `dst_chunk_id` / `meta` already exist (0001). Registers only the relation
-- pair, read at link time by `validate_relation(store=…)` (open vocabulary,
-- ADR 0056), so no core literal edit is needed.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('depicts', FALSE, 'depicted-in',
     'A diagram (figure/mermaid) source chunk depicts the target chunk/ref '
     'it illustrates; the depicting element id(s) live in '
     'links.meta.elements. Diagram→corpus binding (ADR 0057), the '
     'element-granular cousin of plots.'),
    ('depicted-in', FALSE, 'depicts',
     'Source chunk/ref is depicted by the target diagram (inverse of '
     'depicts, ADR 0057).')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- 0154_make_kind_and_made_by.sql
--
-- The make-tree kind + its alignment edge
-- (docs/backlog/make-tree-vs-design-tree.md — the EBOM/MBOM split, and on
-- the molecular side the dual-graph molecule/route pattern):
--
--   * `make` — a slug-addressed ref whose body rides the draft chunk-tree
--     substrate (migration 0031; no new tables): one chunk per STEP,
--     ordered + hierarchical, step conditions in chunk meta, addressed by
--     the stable `mk<chunk_id>` handle. Process, not structure — a
--     strategy OVER a design, so two make-orders (placed assembly vs bulk
--     synthesis) can coexist against the same design tree.
--   * `made-by` / `makes` — block → step alignment, explicitly
--     many-to-many and chunk-scopable (a step may bundle blocks across
--     subsystems; a disconnection may cut inside one block). Ref-level
--     `made-by` (design → make ref) reads "this design is made by this
--     tree". Enters WITH its consumer (CadHandler.link rel='made-by' +
--     MakeHandler's per-step ⛓ block render), per the
--     enter-with-consumer rule; se stays plugin-local by design
--     (design-graph-relations.md revision 2026-09-05).
--
-- Forward-only (ADR 0005). Idempotent (`ON CONFLICT DO NOTHING`).

BEGIN;

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('make', FALSE, 'Make',
     'A make-tree: assembly/synthesis order for a design — first-class '
     'step nodes on the draft chunk-tree substrate, each carrying its '
     'conditions (fixture/torque; reagents/temperature) in chunk meta. '
     'Blocks align to steps via made-by links written from the design '
     'side. Named ref; steps addressed mk<chunk_id>. See precis-cad-help.')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('step', FALSE,
     'One make-tree step (kind=make): an assembly/synthesis action whose '
     'conditions (fixture/torque; reagents/temperature) ride chunk meta; '
     'addressed mk<chunk_id>, aligned to blocks via made-by links.')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('made-by', FALSE, 'makes',
     'Source design/block is produced by the target make-tree (ref-level) '
     'or make-step (chunk-scoped); many-to-many — make-order need not '
     'align with design structure.'),
    ('makes', FALSE, 'made-by',
     'Source make-tree/step produces the target design/block.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0154_make_kind_and_made_by.sql

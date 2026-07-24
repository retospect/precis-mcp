-- 0080_argument_graph_relations.sql
--
-- Argument graph, v1 slice (ADR 0054 — docs/decisions/
-- 0054-argument-graph-lemmas-inferences-reasoning-shadow.md; build plan
-- docs/design/argument-graph.md). Registers the two new link relation
-- pairs the shadow reasoning graph beside a draft needs:
--
--   * `entails`   (inference node → conclusion lemma)   ↔ `entailed-by`
--     "A logically yields B — asserted, not proven." Directed from a
--     `memory` tagged `kind:inference` to the `memory` tagged `kind:lemma`
--     it concludes. Premises attach to the inference via the *reused*
--     `derived-from` relation (inference derived-from each premise) — no
--     new relation needed for that half (ADR 0054 §2, §Risks R2).
--
--   * `qualifies` (caveat node → claim it bounds)        ↔ `qualified-by`
--     "A limits/caveats B." Directed from a `memory` tagged `kind:caveat`
--     to the claim (finding / lemma) it limits. `view='argument'` walks
--     `qualified-by` to surface every caveat a conclusion inherited,
--     marked "inherited — confirm still addressed" — caveats propagate by
--     *display*, never by logic (ADR 0054 §7).
--
-- Both pairs are asymmetric with an inverse, so both directions
-- auto-mirror at *read* time via `links_for` / `relations.inverse_slug` —
-- not write-mirrored (ADR 0054 §2.1). Kept in sync with the `Relation`
-- Literal + `_INVERSE_RELATIONS` map in `store/types.py`.
--
-- No table seed for the new `STALE:` tag axis (ADR 0054 §5/R5, the
-- system-set retraction-ripple marker `STALE:retracted-premise`): there is
-- no `tag_prefixes` table in this schema — `SRC:` / `CACHE:` / `DENSITY:`
-- (the existing system-set axes `STALE:` mirrors) are registered only in
-- the Python-side vocabulary (`store/types.py::_CLOSED_VOCAB` +
-- `_KIND_ALLOWED_AXES`, `store/_mappers.py::_SYSTEM_WRITABLE_PREFIXES`),
-- not a DB row; `STALE:` follows the same, already-established pattern.
--
-- Forward-only (ADR 0005). Idempotent (`ON CONFLICT DO NOTHING`).

BEGIN;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('entails',      FALSE, 'entailed-by',
     'Source inference node logically yields the target conclusion lemma '
     '(asserted, not proven).'),
    ('entailed-by',  FALSE, 'entails',
     'Source lemma is the asserted conclusion of the target inference node.'),
    ('qualifies',    FALSE, 'qualified-by',
     'Source caveat node limits/bounds the target claim (finding or lemma).'),
    ('qualified-by', FALSE, 'qualifies',
     'Source claim is limited/bounded by the target caveat node.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0080_argument_graph_relations.sql

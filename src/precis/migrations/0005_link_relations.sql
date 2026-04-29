-- Phase 7 — fill out the relations vocabulary.
--
-- The initial schema (0001) seeded only ``related-to``, ``blocks`` /
-- ``blocked-by``, and ``contradicts`` / ``contradicted-by``. The
-- agent-facing skill docs (`precis-relations`, `precis-memory-help`,
-- `precis-todo-help`, `precis-navigation`) show ``cites``,
-- ``derived-from``, and ``supports`` in their canonical examples,
-- but writing those today would raise a foreign-key violation
-- against ``links.relation``.
--
-- This migration registers the missing slugs ahead of wiring
-- ``link=`` / ``unlink=`` / ``rel=`` end-to-end. Each carries an
-- explicit inverse where the relation is asymmetric, so a
-- ``cites`` link from A to B can be rendered as ``cited-by`` from
-- B's perspective without storing two rows.
--
-- Inverse handling is **app-level** — the ``relations.inverse_slug``
-- column is documentation, not auto-population. ``put(rel='cites')``
-- inserts exactly one row; the link-renderer queries both
-- directions and uses the inverse_slug to label inbound rows. This
-- matches the schema comment on the column and avoids the
-- consistency problems an auto-mirroring DB trigger would create.

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    -- Citation graph
    ('cites',         FALSE, 'cited-by',     'Source cites target'),
    ('cited-by',      FALSE, 'cites',        'Source is cited by target'),
    -- Provenance / derivation
    ('derived-from',  FALSE, 'derived-into', 'Source was derived from target'),
    ('derived-into',  FALSE, 'derived-from', 'Source led to derived target'),
    -- Evidential support
    ('supports',      FALSE, 'supported-by', 'Source supports target claim'),
    ('supported-by',  FALSE, 'supports',     'Source claim is supported by target'),
    -- Generalisation / specialisation
    ('generalises',   FALSE, 'specialises',  'Source generalises target'),
    ('specialises',   FALSE, 'generalises',  'Source specialises target'),
    -- See also: weaker than related-to, asymmetric (no inverse).
    -- Useful when one ref points TO another for context without
    -- claiming a peer relationship.
    ('see-also',      FALSE, NULL,           'Source points to target for context')
ON CONFLICT (slug) DO NOTHING;

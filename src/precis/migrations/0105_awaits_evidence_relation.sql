-- 0105_awaits_evidence_relation.sql
--
-- Acquisition-mode findings (docs/proposals/finding-acquisition-mode.md
-- §2) — a new relation slug for the finding→stub-paper edge.
--
-- `put(kind='finding', wants=[...], provenance=...)` mints a finding
-- STATUS:acquiring plus one DREAM:acquire paper stub per `wants=`
-- descriptor, and links `finding --awaits-evidence--> stub` so the chase
-- worker (`workers/chase.py`) can poll each stub for chunks once
-- `fetch_oa` lands its PDF. `links.relation` FKs to `relations(slug)`
-- (`links_relation_fkey`) — a closed, migration-seeded vocabulary, not
-- free text — so the new slug needs its own row (same trap as
-- `establishes`, migration 0094 / ADR 0073, per
-- docs/decisions/0073-taproot-evidence-relations.md).
--
-- No inverse: the finding reads its awaits-evidence stubs via
-- `links_for(finding, direction='out', relation='awaits-evidence')` —
-- direction filtering, not auto-mirroring — matching the asymmetric
-- no-inverse convention `establishes` (0094) and `corroborates` (0085)
-- already use for evidence-shaped edges. Keep in sync with the
-- `Relation` Literal in `store/types.py`.
--
-- Forward-only (ADR 0005). Idempotent (`ON CONFLICT DO NOTHING`).

BEGIN;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('awaits-evidence', FALSE, NULL,
     'An acquisition-mode finding (STATUS:acquiring) awaits corpus '
     'evidence from the linked DREAM:acquire paper stub.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0105_awaits_evidence_relation.sql

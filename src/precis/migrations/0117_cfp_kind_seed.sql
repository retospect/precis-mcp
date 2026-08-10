-- 0117_cfp_kind_seed.sql
--
-- Fix gr194088: the `cfp` kind (shipped 2026-06-27, migration
-- 0040_cfp_requirement_relation.sql) never got a migration-seeded
-- `kinds` row of its own — that migration only seeded the
-- `has-requirement`/`requirement-of` relations. Since ``_kinds_ops.py``'s
-- boot-time ``upsert_kinds`` (2026-06-16 cutover) is the canonical path,
-- prod's `kinds` table has the `cfp` row (upserted at every boot), but
-- ``precis db dump-schema`` builds the baseline by replaying *only* the
-- migration chain (``Migrator.apply_all``, no dispatch boot) — so the
-- committed ``migrations/baseline/schema.sql`` snapshot, and any fresh/
-- test DB loaded from it, is missing the row and `insert_ref(kind='cfp',
-- ...)` fails with `unknown kind: 'cfp'`.
--
-- Mirrors the belt-and-suspenders precedent set by 0092_material_kind.sql
-- / 0093_component_kind.sql: seed the row via migration (idempotent,
-- ``ON CONFLICT DO NOTHING``) so both the replay-built baseline and any
-- DB migrated from scratch carry it, on top of the boot-time upsert
-- keeping title/description in sync going forward. Values match
-- ``CfpHandler.spec`` (``precis/handlers/cfp.py``).
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('cfp', FALSE, 'Call for Proposal',
     'Call-for-proposal / requirements document. A read-only ingested '
     'PDF (via `precis add --as cfp` or the inbox/cfp/ watch dir) that '
     'a proposal draft must satisfy. Addressable by slug; one ref per '
     'document, blocks per chunk — gets search / TOC / keywords like a '
     'paper. Spec role: NEVER citable evidence (it is the requirements, '
     'not a source). Link it to a proposal project with '
     'link(rel=''has-requirement'') so the planner consults it. Use '
     'get(view=''toc'') to read the required sections + limits.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0117_cfp_kind_seed.sql

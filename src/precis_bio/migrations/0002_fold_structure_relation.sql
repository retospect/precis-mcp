-- precis_bio/0002_fold_structure_relation.sql
--
-- The `has-fold-structure` / `fold-structure-of` relation pair (ADR 0056 slice
-- 4c): links a `protein` to the derived `structure` ref that projects its folded
-- mmCIF into the 3D viewer (get(kind='protein', id=…, view='structure')). The
-- fold IR stays on the protein's meta.fold; the structure is an on-demand,
-- content-slugged projection, so the edge lets each find the other.
--
-- ASYMMETRIC with a DB inverse_slug — deliberately, now that gripe 160213 is
-- fixed (Store.inverse_relation reads relations.inverse_slug, so a plugin
-- relation's inverse mirrors on the links_for read filter). Before that fix a
-- plugin relation had to be symmetric; this pair is the first to rely on the
-- new DB-sourced inverse.
--
-- Forward-only (ADR 0005). Idempotent. Plugin migration (namespace precis_bio),
-- applied after core so the relations reference table exists.

BEGIN;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('has-fold-structure', FALSE, 'fold-structure-of',
     'A protein points to the derived structure ref that projects its folded '
     'mmCIF into the 3D viewer (ADR 0056 slice 4c).'),
    ('fold-structure-of', FALSE, 'has-fold-structure',
     'Inverse of has-fold-structure: the structure projection points back to '
     'the protein it was folded from.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

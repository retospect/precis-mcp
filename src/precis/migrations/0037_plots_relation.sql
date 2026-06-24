-- 0037_plots_relation.sql
--
-- ADR 0035 §2/§4 — the `plots` link relation: a figure chunk renders one or
-- more data (table) chunks. This is the **one live, reactive recompute edge**
-- in the computed-chunk design — editing a plotted data chunk marks its
-- figures stale (a render follows). Every other edge (`derived-from`, `regen`,
-- `cites`) is inert.
--
-- A chunk→chunk link: `src_chunk_id` = the figure, `dst_chunk_id` = the data
-- chunk (the links table already carries both, ADR 0033/v2). Asymmetric with a
-- `plotted-by` inverse, so the stale-walk reads "which figures plot this data?"
-- via `links_for(data_chunk, relation='plots', direction='in')`.
--
-- Forward-only (ADR 0005). Idempotent under ON CONFLICT DO NOTHING. Keep the
-- `Relation` literal in store/types.py in sync. Regenerate the baseline
-- snapshot at release (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('plots', FALSE, 'plotted-by',
     'Source figure chunk renders the target data chunk — the figure plots '
     'that data. The one reactive edge: editing the data marks the figure '
     'stale (ADR 0035).'),
    ('plotted-by', FALSE, 'plots',
     'Source data chunk is rendered by the target figure chunk (inverse of '
     'plots).')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0037_plots_relation.sql

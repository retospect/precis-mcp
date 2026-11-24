-- 0366_figure_elements.sql
BEGIN;

INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('figure_element', FALSE, 'A top-level SVG element on an editable figure.')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO relations (slug, is_symmetric, description) VALUES
    ('figure-in', FALSE, 'Source figure is embedded at the target draft figure chunk.')
ON CONFLICT (slug) DO NOTHING;

-- Give existing draft canvases the same outgoing edge as new attachments.
INSERT INTO links (src_ref_id, dst_ref_id, dst_chunk_id, relation, set_by)
SELECT l.dst_ref_id, l.src_ref_id, l.src_chunk_id, 'figure-in', l.set_by
FROM links l
JOIN refs draft ON draft.ref_id = l.src_ref_id AND draft.kind = 'draft'
JOIN refs canvas ON canvas.ref_id = l.dst_ref_id AND canvas.kind = 'figure'
JOIN chunks anchor ON anchor.chunk_id = l.src_chunk_id
WHERE l.relation = 'has-figure'
  AND l.dst_chunk_id IS NULL
  AND draft.retired_at IS NULL
  AND canvas.retired_at IS NULL
  AND anchor.retired_at IS NULL
  AND anchor.chunk_kind = 'figure'
ON CONFLICT DO NOTHING;

COMMIT;

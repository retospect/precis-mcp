-- scripts/v_refs_dup_identifiers.sql
--
-- gr516504 step 2 dry list — READ ONLY. Run:
--   scripts/prod-psql --ro < scripts/v_refs_dup_identifiers.sql
--
-- For every ref holding more than one ref_identifiers row of a
-- "single-valued" kind (pub_id, cite_key, paper_id — the three v_refs
-- reads as scalars), list each duplicate value, which row the
-- deterministic v_refs pick now exposes (migration
-- 0246_v_refs_deterministic_identifiers.sql), and which rows a dedupe
-- pass would drop. Output feeds the canonical-row ruling; nothing
-- here writes.
--
-- THIS IS NOT A DELETE LIST. The extras are largely intentional:
--   * pub_id: a taproot reword mints a fresh pub_id and deliberately
--     keeps the old one so drafts already citing it keep resolving
--     (Store.current_pub_ids) — "extras" are resolution history, and
--     the dedupe ruling may mean deleting only a subset or none.
--   * cite_key: the second row is typically a 'local' short key and a
--     'tex-import' long key minted for the same ref — they read like
--     aliases, not corruption; mentions.py matches ANY cite_key row,
--     so both are live citation addresses.
-- The step-2 ruling decides what canonical means per kind before any
-- row is deleted.
--
-- The pick mirrors the view exactly: pub_id takes created_at DESC,
-- cite_key/paper_id created_at ASC, id_value the tiebreak either way.

-- Section 1: per-kind roll-up — dup groups, rows involved, extras a
-- dedupe would remove.
WITH dup AS (
    SELECT ref_id, id_kind
    FROM ref_identifiers
    WHERE id_kind IN ('pub_id', 'cite_key', 'paper_id')
    GROUP BY ref_id, id_kind
    HAVING count(*) > 1
)
SELECT
    d.id_kind,
    count(*)               AS dup_groups,
    sum(c.n)               AS rows_in_dups,
    sum(c.n) - count(*)    AS extra_rows_a_dedupe_drops
FROM dup d
JOIN (
    SELECT ref_id, id_kind, count(*) AS n
    FROM ref_identifiers
    WHERE id_kind IN ('pub_id', 'cite_key', 'paper_id')
    GROUP BY ref_id, id_kind
) c USING (ref_id, id_kind)
GROUP BY d.id_kind
ORDER BY d.id_kind;

-- Section 2: the per-row list — picked_by_v_refs marks the row the
-- view exposes; the rest are the extras a dedupe deletes once the
-- canonical row is confirmed.
WITH ranked AS (
    SELECT
        ri.ref_id,
        ri.id_kind,
        ri.id_value,
        ri.source,
        ri.created_at,
        row_number() OVER (
            PARTITION BY ri.ref_id, ri.id_kind
            ORDER BY CASE WHEN ri.id_kind = 'pub_id'
                          THEN ri.created_at END DESC NULLS LAST,
                     CASE WHEN ri.id_kind <> 'pub_id'
                          THEN ri.created_at END ASC NULLS LAST,
                     ri.id_value
        ) AS rn,
        count(*) OVER (PARTITION BY ri.ref_id, ri.id_kind) AS n_rows
    FROM ref_identifiers ri
    WHERE ri.id_kind IN ('pub_id', 'cite_key', 'paper_id')
      AND (ri.ref_id, ri.id_kind) IN (
          SELECT ref_id, id_kind
          FROM ref_identifiers
          WHERE id_kind IN ('pub_id', 'cite_key', 'paper_id')
          GROUP BY ref_id, id_kind
          HAVING count(*) > 1
      )
)
SELECT ref_id, id_kind, id_value, source, created_at,
       (rn = 1) AS picked_by_v_refs, n_rows
FROM ranked
ORDER BY id_kind, ref_id, rn;

-- End of scripts/v_refs_dup_identifiers.sql

-- 0327_repair_zeolite_metadata.sql
-- gr681475: pa5022 holds the zeolite review but inherited Wang's metadata.
-- Evidence: https://pubs.rsc.org/en/content/articlelanding/2015/cs/c5cs00029g
-- Guard by record, DOI and held text; fresh databases do nothing. Preserve
-- old keys as aliases and unrelated metadata. Never change body chunks.
BEGIN;
SET LOCAL lock_timeout = '5s';

DO $$
BEGIN
    PERFORM 1 FROM refs r
    WHERE r.ref_id = 5022 AND r.kind = 'paper'
      AND EXISTS (SELECT 1 FROM ref_identifiers i WHERE i.ref_id = r.ref_id
                  AND i.id_kind = 'doi' AND lower(i.id_value) = '10.1039/c5cs00029g')
      AND EXISTS (SELECT 1 FROM chunks c WHERE c.ref_id = r.ref_id AND c.ord = 0
                  AND c.text ILIKE '%Advances in theory and their application within the field of zeolite chemistry%')
    FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;

    UPDATE refs SET
        title = 'Advances in theory and their application within the field of zeolite chemistry',
        year = 2015,
        meta = coalesce(meta, '{}'::jsonb) ||
            '{"journal":"Chemical Society Reviews","entry_type":"journal-article","volume":"44","number":"20","pages":"7044-7111","metadata_repair":"gr681475"}'::jsonb,
        updated_at = now()
    WHERE ref_id = 5022;

    DELETE FROM paper_authors WHERE ref_id = 5022;
    INSERT INTO paper_authors (ref_id, position, given, family, name_raw, source)
    VALUES
        (5022, 1, 'Veronique', 'Van Speybroeck', 'Veronique Van Speybroeck', 'crossref'),
        (5022, 2, 'Karen', 'Hemelsoet', 'Karen Hemelsoet', 'crossref'),
        (5022, 3, 'Lennart', 'Joos', 'Lennart Joos', 'crossref'),
        (5022, 4, 'Michel', 'Waroquier', 'Michel Waroquier', 'crossref'),
        (5022, 5, 'Robert G.', 'Bell', 'Robert G. Bell', 'crossref'),
        (5022, 6, 'C. Richard A.', 'Catlow', 'C. Richard A. Catlow', 'crossref');
    UPDATE refs SET authors = (
        SELECT jsonb_agg(jsonb_build_object('given', given, 'family', family)
                         ORDER BY position)
        FROM paper_authors WHERE ref_id = 5022
    ) WHERE ref_id = 5022;

    INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source)
    SELECT 'cite_key_alias', id_value, ref_id, 'gr681475'
    FROM ref_identifiers WHERE ref_id = 5022 AND id_kind = 'cite_key'
      AND id_value <> 'vanspeybroeck15-pa5022'
    ON CONFLICT (id_kind, id_value) DO NOTHING;
    DELETE FROM ref_identifiers WHERE ref_id = 5022 AND id_kind = 'cite_key';
    INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source)
    VALUES ('cite_key', 'vanspeybroeck15-pa5022', 5022, 'gr681475');
END $$;
COMMIT;

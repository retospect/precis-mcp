-- 0049_doi_lowercase_guard.sql
--
-- DOIs are case-insensitive (DOI Handbook §2.4) and `identity.normalize_doi`
-- canonicalises them to lowercase, but several identifier-insert paths
-- (watch_poll citation stubs, orcid import, draftimport bib resolve) wrote the
-- publisher's verbatim DOI straight into `ref_identifiers` without
-- normalising. Because the lookup that upgrades a stub to a full paper is an
-- *exact* `id_value =` match and the PK is `(id_kind, id_value)`, a stub minted
-- with `.../d19-1371` never met an ingested PDF's `.../D19-1371`: the paper
-- landed as a second ref and the stub stayed on the "papers we still need to
-- get" backlog forever. On prod this stranded ~117 already-ingested papers
-- (~23% of the stub backlog) as ghost wants beside their real, chunked twin.
--
-- Make a non-lowercase DOI unrepresentable at the storage layer, so no future
-- insert path can reintroduce the split regardless of whether its Python
-- caller remembered to normalise:
--
--   * a BEFORE INSERT OR UPDATE trigger lowercases `id_value` when
--     `id_kind = 'doi'`. It heals the write rather than rejecting it, so a
--     stray raw insert can't break the ingest pipeline — it just gets
--     canonicalised. This is the real enforcement.
--   * a CHECK constraint asserts the invariant (belt to the trigger's
--     suspenders — it also guards against the trigger being dropped). It is
--     added NOT VALID because the ~403 legacy non-lowercase rows still on prod
--     would fail an immediate validation, and lowercasing them in place
--     collides on the PK with their already-lowercased stub twins. That
--     collision *is* the duplicate pair: `precis reconcile-duplicates` folds
--     the stub into the chunked survivor (via the tested merge_duplicate
--     primitive) and then lowercases the survivor's DOI. After that runs:
--       ALTER TABLE ref_identifiers VALIDATE CONSTRAINT ref_identifiers_doi_lc;
--     New rows are enforced from the moment this migration lands (the trigger
--     normalises, so the NOT VALID check always passes on write).
--
-- The Python read/write chokepoints (store.upsert_stub_paper,
-- workers.watch_poll, workers.chase) are normalised in the same change so
-- stub-collapse *probes* also match the now-canonical rows — the trigger fixes
-- storage, but a probe that searches for a raw `D19-1371` would still miss a
-- lowercased row.

BEGIN;

CREATE OR REPLACE FUNCTION ref_identifiers_lowercase_doi()
    RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.id_kind = 'doi' THEN
        NEW.id_value := lower(NEW.id_value);
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_ref_identifiers_lowercase_doi ON ref_identifiers;
CREATE TRIGGER trg_ref_identifiers_lowercase_doi
    BEFORE INSERT OR UPDATE ON ref_identifiers
    FOR EACH ROW
    EXECUTE FUNCTION ref_identifiers_lowercase_doi();

ALTER TABLE ref_identifiers
    ADD CONSTRAINT ref_identifiers_doi_lc
    CHECK (id_kind <> 'doi' OR id_value = lower(id_value))
    NOT VALID;

COMMIT;

-- End of 0049_doi_lowercase_guard.sql

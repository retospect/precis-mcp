-- 0359_doi_trailing_punct_guard.sql
--
-- gr763049: a DOI copied out of a citation string can carry prose
-- typesetting on its tail — pa2193 stored `10.1149/2.1221816jes]`
-- beside pa2169's `10.1149/2.1221816jes`. The exact-match dedup probes
-- never met (one byte differs), so both refs stayed live and full-text
-- for the same paper. Migration 0049 made a non-lowercase DOI
-- unrepresentable at storage; do the same for trailing citation
-- punctuation — `.,;:`, whitespace, and a closing bracket no opener
-- inside the value balances (the `identity.clean_doi` rule: DOI
-- suffixes legitimately carry *balanced* parens, gr658687).
--
--   * `ref_identifiers_doi_canonical(text)` — the storage canonical
--     form, IMMUTABLE so the trigger, the CHECK and the audit all share one rule.
--   * the existing BEFORE INSERT OR UPDATE trigger routes through it,
--     so a raw write can no longer store the split.
--   * a NOT VALID CHECK asserts the invariant; `precis
--     reconcile-duplicates --apply` preserves case-only reconciliation
--     (corroboration-gated, gr501446) and canonicalises lone dirty
--     rows, then the sweep validates the constraint itself (or by
--     hand: ALTER TABLE ref_identifiers VALIDATE CONSTRAINT
--     ref_identifiers_doi_clean).
--   * the same command prints the audit — every stored DOI whose tail
--     a DOI cannot carry — so pairs needing a manual `merge_into`
--     can be reviewed.
--
-- The Python canonicalisers (`identity.normalize_doi`,
-- `_normalise_identifier`) strip the same tail so probes keep matching
-- the stored form. Forward-only (ADR 0005). Idempotent.

BEGIN;

CREATE OR REPLACE FUNCTION ref_identifiers_doi_canonical(v text)
    RETURNS text
    LANGUAGE plpgsql
    IMMUTABLE
AS $$
DECLARE
    closer text;
    opener text;
BEGIN
    IF v IS NULL THEN
        RETURN NULL;
    END IF;
    v := lower(btrim(v, E' \t\n\r\f\x0b'));
    LOOP
        v := rtrim(v, E'.,;: \t\n\r\f\x0b');
        EXIT WHEN v = '';
        closer := right(v, 1);
        opener := CASE closer
            WHEN ')' THEN '('
            WHEN ']' THEN '['
            WHEN '}' THEN '{'
            WHEN '>' THEN '<'
            ELSE NULL
        END;
        EXIT WHEN opener IS NULL
              OR (length(v) - length(replace(v, closer, '')))
                 <= (length(v) - length(replace(v, opener, '')));
        v := left(v, -1);
    END LOOP;
    RETURN v;
END;
$$;

-- Route the 0049 trigger through the canonical form (name kept: the
-- trigger row and its dependents stay untouched).
CREATE OR REPLACE FUNCTION ref_identifiers_lowercase_doi()
    RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.id_kind = 'doi' THEN
        NEW.id_value := ref_identifiers_doi_canonical(NEW.id_value);
    END IF;
    RETURN NEW;
END;
$$;

ALTER TABLE ref_identifiers
    ADD CONSTRAINT ref_identifiers_doi_clean
    CHECK (id_kind <> 'doi' OR id_value = ref_identifiers_doi_canonical(id_value))
    NOT VALID;

COMMIT;

-- End of 0359_doi_trailing_punct_guard.sql

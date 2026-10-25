-- 0248_vault_set_secret_if_absent.sql
--
-- Close the first-mint race in vault writes (gr517874).
--
-- vault.set_secret is an upsert (ON CONFLICT DO UPDATE, 0059): two
-- processes minting a secret on first use each store their own value and
-- the last writer wins silently. That is harmless for a rotation but
-- fatal when the minted value derives other state — a password hash made
-- with the losing PRECIS_WEB_PASSWORD_PEPPER is permanently unverifiable,
-- and a link key overwrite 403s every URL signed before it.
--
-- vault.set_secret_if_absent makes the first insert stick and reports
-- only whether *this* call wrote it — it never returns plaintext, so
-- vault.reveal stays the single decrypt path (0059, ADR 0055). ON
-- CONFLICT DO NOTHING under the primary key serializes the race (a loser
-- blocks on the winner's uncommitted insert, then skips), and the caller
-- reads the stored value back through vault.reveal — a normally-audited
-- reveal carrying the 0111 client identity, so a future grant-narrowing
-- that covers reveal needs no special case for this verb.
--
-- Audit shape follows the existing verbs: the winning insert logs 'set'.
-- The loser writes no event here — its decrypt happens in the caller's
-- own vault.reveal. There is no NULL return: a delete_secret racing the
-- pair is detected by the caller's read-back finding no row, and the
-- caller must not trust its minted value in that case.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

CREATE OR REPLACE FUNCTION vault.set_secret_if_absent(p_name text, p_value text)
    RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = vault, public, pg_temp AS $$
BEGIN
    IF p_value IS NULL OR length(p_value) = 0 THEN
        RAISE EXCEPTION 'vault: refusing to store an empty value for %', p_name;
    END IF;
    INSERT INTO vault.secrets(name, ciphertext, hint)
    VALUES (p_name,
            pgp_sym_encrypt(p_value, vault._key()),
            vault._hint(p_value))
    ON CONFLICT (name) DO NOTHING;
    IF FOUND THEN
        INSERT INTO vault.events(who, verb, name)
        VALUES (session_user, 'set', p_name);
        RETURN true;
    END IF;
    RETURN false;
END
$$;

REVOKE ALL ON FUNCTION vault.set_secret_if_absent(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION vault.set_secret_if_absent(text, text) TO PUBLIC;

COMMIT;

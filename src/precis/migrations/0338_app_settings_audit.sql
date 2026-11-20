-- 0338_app_settings_audit.sql
--
-- Audit trail for app_settings writes (gr635487): who, when, old -> new,
-- why, the motivating ref and the user's quoted confirmation. Written by
-- precis.settings.set_setting / clear_setting in the write's transaction.
CREATE TABLE IF NOT EXISTS app_settings_audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    key text NOT NULL,
    old_value text,
    new_value text,
    actor text NOT NULL,
    reason text,
    motivating_ref text,
    confirmed_quote text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS app_settings_audit_key_idx
    ON app_settings_audit (key, id DESC);

-- End of 0338_app_settings_audit.sql

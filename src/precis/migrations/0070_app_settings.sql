-- 0070_app_settings.sql
--
-- A tiny key/value store for web-editable runtime settings that override an
-- env default without a redeploy (docs/design/budget-guardrails.md, Piece B).
-- First consumer: the budget circuit breaker's caps
-- (`budget.hourly_usd` / `budget.daily_usd`), set from the /budget page. The
-- DB value overrides the PRECIS_BUDGET_* env default; env remains the boot
-- floor when no row exists.
--
-- Deliberately generic (string key → string value) so later web-editable
-- knobs reuse it rather than each growing a bespoke column. Values are
-- plaintext by design — this is NOT for secrets (those live encrypted in the
-- vault, migration/ADR 0055). Idempotent: IF NOT EXISTS so re-running and
-- fresh installs no-op cleanly.

CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

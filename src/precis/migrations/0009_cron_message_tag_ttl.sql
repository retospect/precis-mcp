-- 0009_cron_message_tag_ttl.sql
--
-- Three additions bundled because they ship as one feature: Asa-on-Claude
-- as a hosted Discord agent that uses precis for all state.
--
-- 1. `kind='cron'` — scheduled wakeup. The precis cron-tick CLI (launchd
--    timer every 60s) scans for due entries, fires pg_notify('precis.cron'),
--    advances next_fire_at per recurrence + catch_up policy. asa_bot
--    LISTENs and wakes Asa with the payload as a synthetic prompt.
--    Slug-addressed. State lives in meta JSON: next_fire_at,
--    last_fired_at, recurring, catch_up, payload, target. Status as
--    meta.status='scheduled'|'fired'|'expired'|'cancelled'|'paused' —
--    not a closed-tag axis so the kind stays flexible.
--
-- 2. `kind='message'` — proactive outbound. asa_bot calls put(kind='message',
--    target='discord/G/C/T', text='...'); the handler fires
--    pg_notify('precis.messages') and asa_bot delivers. Every send becomes
--    a stored, searchable ref ("what have I been nagging the user about?").
--
-- 3. `ref_tags.expires_at` — TTL on tags. Enables sticky:thread / sticky:global
--    with auto-decay. Generic capability — any tag axis can now carry a TTL
--    (`STATUS:doing` auto-stale, `WATCH:hourly` time-bounded, etc).
--    Query-time filter excludes expired tags from search results; expired
--    rows stay in the table for audit + revival.
--
-- Forward-only (ADR 0005). Idempotent ON CONFLICT for seed rows;
-- IF NOT EXISTS on schema changes.

BEGIN;

-- ===========================================================================
-- 1. kind='cron' registration
-- ===========================================================================

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('cron', TRUE, 'Cron',
     'Scheduled wakeup. The cron-tick CLI scans due entries every '
     '60s, fires pg_notify(''precis.cron''), advances next_fire_at '
     'per recurrence + catch_up policy. Numeric-id; body lives as a '
     '``cron_payload`` chunk. State in meta.next_fire_at, '
     'meta.recurring, meta.catch_up, meta.status. See '
     '``precis-cron-help``.')
ON CONFLICT (slug) DO NOTHING;

-- ===========================================================================
-- 2. kind='message' registration
-- ===========================================================================

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('message', TRUE, 'Message',
     'Proactive outbound. put(kind=''message'', target=''discord/G/C/T'', '
     'text=''...'') stores the ref AND fires pg_notify(''precis.messages''). '
     'Delivery layer (asa_bot) LISTENs and posts. Numeric-id; one ref '
     'per send. Body as ``message_body`` chunk. State in meta.status: '
     '''queued'' → ''sent''/''failed''. See ``precis-message-help``.')
ON CONFLICT (slug) DO NOTHING;

-- ===========================================================================
-- 3. New chunk_kinds for body storage on cron + message
-- ===========================================================================

INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('cron_payload', FALSE,
     'Cron entry body — the natural-language payload that becomes the '
     'synthetic prompt to Asa when the cron fires. Searchable; embed + '
     'chunk_keywords workers index it normally.'),
    ('message_body', FALSE,
     'Outbound message body. The text that gets posted. Searchable so '
     'past sends can be retrieved with search(kind=''message'', q=''...'').')
ON CONFLICT (slug) DO NOTHING;

-- ===========================================================================
-- 4. ref_tags.expires_at — TTL on tags
-- ===========================================================================

-- Optional expiry timestamp. NULL = no expiry (the prior behaviour, default
-- for every existing row). Set at write time via tag(ttl_days=N) or
-- tag(expires_at='...'). Query path adds a `WHERE expires_at IS NULL OR
-- expires_at > now()` filter on tag-aware reads.
--
-- Set NOT NULL DEFAULT to keep schema deterministic and explicit:
-- agents reading the column on legacy rows should see NULL (no expiry),
-- not an unexpected timestamp.
ALTER TABLE ref_tags
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

-- Partial index — only rows that carry an expiry are indexed. Two query
-- patterns this serves:
--   * "find expired tags" (housekeeping sweep)
--   * "include only non-expired tags" (the runtime filter)
-- Both benefit; rows without expiry (the common case) cost nothing.
CREATE INDEX IF NOT EXISTS ref_tags_expires_at_idx
    ON ref_tags (expires_at)
    WHERE expires_at IS NOT NULL;

COMMIT;

-- End of 0009_cron_message_tag_ttl.sql

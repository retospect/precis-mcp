-- 0029_alert_kind.sql
--
-- Register `kind='alert'` — a first-class home for machine-detected
-- operational / health conditions (worker spin loops, orphaned todos,
-- stalled recurrings, stale claims, …). Previously the nursery pass
-- wrote these as `kind='memory'` rows tagged `internal-thought`, which
-- conflated ops telemetry with reflective *thought*: it polluted the
-- memory namespace (thousands of admin rows) and forced a TTL purge +
-- fingerprint dedup + a write throttle to manage records that are pure
-- derived state. `alert` separates the two — alerts are deduped on a
-- `meta.fingerprint`, carry a `STATUS:` lifecycle (open → resolved),
-- and are surfaced by the web `/alerts` tab, not by semantic search.
--
-- Shape: numeric-id, note-like — same family as gripe / job / todo /
-- memory. The title carries the human-readable headline; `meta` JSONB
-- carries `alert_source`, `severity`, `fingerprint`, `subject_ref_id`,
-- and recurrence counters (`first_seen` / `last_seen` / `seen_count`).
-- No new columns and no chunk_kind: alerts are intentionally NOT
-- embedded (no `card_combined`), so the embed / chunk_keywords workers
-- never touch them. The body lives entirely in `refs.title` + `meta`.
--
-- Forward-only (ADR 0005). Idempotent under `ON CONFLICT DO NOTHING`.

BEGIN;

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('alert', TRUE, 'Alert',
     'Machine-detected operational / health condition — a worker spin '
     'loop, an orphaned todo, a stalled recurring, a stale claim. '
     'Addressable by numeric id; deduped on meta.fingerprint; '
     'lifecycle via STATUS: tags (open / resolved); source + severity '
     'via alert-source: / severity: open tags. Not embedded — surfaced '
     'by the /alerts web tab, not semantic search.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0029_alert_kind.sql

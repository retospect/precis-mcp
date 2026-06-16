-- 0020_claude_quota_snapshot — singleton snapshot of Claude.ai OAuth limits.
--
-- Anthropic returns ``anthropic-ratelimit-unified-{5h,7d}-utilization`` +
-- ``-reset`` on every API response; Claude Code's ``-p ... --output-format
-- json`` surfaces them as ``.rate_limits.{five_hour,seven_day}.used_percentage``
-- / ``.resets_at``. The agent-profile ``quota_check`` worker pass calls a
-- 1-token completion every N minutes (cheap; the binding window is what
-- the user actually cares about) and writes the parsed snapshot here.
--
-- Singleton-per-scope: ``scope='unified'`` is the live snapshot the Status
-- tab renders. Future scopes (per-OAuth-identity if we ever run multiple)
-- would land as additional rows.
--
-- Why a tiny dedicated table instead of ``host_heartbeat.meta``:
-- conceptually different — heartbeat is per-host liveness, this is per
-- OAuth-identity quota. Cleaner separation; cleaner queries on the
-- Status tab read path.

CREATE TABLE claude_quota_snapshot (
    scope TEXT NOT NULL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL,
    data JSONB NOT NULL DEFAULT '{}'
);

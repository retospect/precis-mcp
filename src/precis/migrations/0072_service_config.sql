-- 0072_service_config.sql
--
-- The factory `service_config` table — live, DB-driven control of which
-- worker passes run where, at what claim weight, on which model
-- (docs/design/factory-console-and-scheduling.md, slice 2). Before it, a
-- pass gate was a plist `EnvironmentVariables` entry: flipping it meant
-- edit-host_var → re-render → launchctl bootout/bootstrap. This row is the
-- switch the worker consults *live* — a flip takes effect on the next loop
-- cycle, no redeploy.
--
-- `prio` is BOTH the switch and the scheduling weight (§7):
--   * 0        — do not run (the live off switch),
--   * 1..10    — run at this claim weight (feeds the scarcity+prio+age
--                claim ordering the capability scheduler adds in slice 6).
-- A missing row means "fall back to the env/profile default" — so an empty
-- table is byte-identical to today's behaviour (the resolver defaults a
-- profile/enable_env pass ON at weight 5, everything else OFF). The default
-- 5 mirrors the mid-point of the refs.prio 1..10 scale quests already use.
--
-- `host` is either a concrete host name (`melchior`) or the wildcard `*`
-- meaning "all hosts"; an exact-host row wins over the wildcard. `model_pref`
-- and `write_level` are provisioned here for slices 4 (model picker, wired
-- through the `llm` catalog) and 8 (per-todo write envelope) — created now
-- so the console has stable columns, but only `prio` is consulted in slice 2.
--
-- Forward-only (ADR 0005): additive, no data migration.

CREATE TABLE IF NOT EXISTS service_config (
    host        TEXT        NOT NULL,
    service     TEXT        NOT NULL,
    prio        INT         NOT NULL DEFAULT 5 CHECK (prio >= 0 AND prio <= 10),
    model_pref  TEXT,
    write_level TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor       TEXT,
    PRIMARY KEY (host, service)
);

COMMENT ON TABLE service_config IS
    'Live per-host per-service run control: prio 0=off, 1..10=claim weight; '
    'host=''*'' is the all-hosts default (exact host wins). Absent row → '
    'env/profile fallback. Factory console slice 2.';

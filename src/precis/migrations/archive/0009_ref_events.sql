-- ===========================================================================
-- 0009_ref_events.sql — cross-subsystem per-ref event log.
--
-- Backs the audit trail every long-lived worker / API consumer in
-- precis writes to. Replaces the would-be ``meta.chase_log`` JSONB
-- array per-kind with a single chronological, queryable table.
--
-- Consumers (write side):
--   * chase                 — finding-chase decisions per pass
--   * fetcher:unpaywall     — OA PDF fetch attempts per stub
--   * provenance:crossref   — Crossref DOI lookup outcomes (future)
--   * worker:embed          — opt-in latency / cost tracing (future)
--
-- Consumers (read side):
--   * get(kind='finding', id=N, view='log')  — per-finding timeline
--   * get(kind='paper',   id=N, view='log')  — ingest + segment +
--                                              fetcher history
--   * precis stubs                            — stub fetch backlog
--   * cross-subsystem queries (incident timelines, cost roll-ups)
--
-- Retention: 90 days. A trim job (TBD: precis maintenance run)
-- prunes rows older than that.
-- ===========================================================================


CREATE TABLE ref_events (
    event_id    BIGSERIAL PRIMARY KEY,
    ref_id      BIGINT  NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Free-text source slug — convention is ``<subsystem>`` for the
    -- whole subsystem (``chase``) or ``<subsystem>:<provider>`` when
    -- multiple providers share one subsystem (``fetcher:unpaywall``,
    -- ``provenance:crossref``). New subsystems pick a slug and start
    -- writing; no registry coupling — the slug is self-documenting.
    source      TEXT    NOT NULL,
    -- Free-text event slug — convention is the verb that happened
    -- (``advanced``, ``terminated``, ``fetch_ok``, ``no_oa_version``).
    -- Per-subsystem vocabulary; readers grep against ``(source, event)``.
    event       TEXT    NOT NULL,
    payload     JSONB,
    duration_ms INT,
    cost_usd    NUMERIC
);

-- Per-ref chronological reads are the dominant query shape (view=log
-- on a finding / paper). Index ts DESC so ORDER BY ts DESC LIMIT N
-- is an index scan.
CREATE INDEX ref_events_ref_id_ts_idx
    ON ref_events (ref_id, ts DESC);

-- Cross-ref queries by subsystem ("everything the fetcher did in the
-- last hour") need a separate index. Compound (source, event, ts)
-- so the common ``WHERE source=X AND event=Y`` case is one probe.
CREATE INDEX ref_events_source_event_ts_idx
    ON ref_events (source, event, ts DESC);


-- ===========================================================================
-- End of 0009_ref_events.sql
-- ===========================================================================

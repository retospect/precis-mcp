-- 0133_tool_calls_ledger.sql
--
-- Per-tool-call telemetry (docs/backlog/mcp-tool-ledger.md). Before this,
-- the live ``precis serve`` path logged nothing structured about individual
-- verb calls, so "which verb/kind/arg-shape confuses agents" required a
-- forensic transcript-mining pass over ``plan_tick`` jobs (~130k subagent
-- tokens per pass, and blind to interactive sessions — only 4.9% of
-- transcript-bearing jobs even contain tool calls).
--
-- One row per ``runtime.dispatch()`` call (see precis/tool_ledger.py, wired
-- at the ``DispatchMixin.dispatch_with_status`` chokepoint every verb call
-- passes through — MCP server, CLI, and in-process agent ticks alike).
--
-- ``input_keys`` is a JSONB array of the top-level input kwarg **names**
-- only — never values, never nested bodies. This is the corpus-safety
-- boundary the backlog item calls out: no payload content, ever, in any
-- column. See the module docstring for the analytics query shape.
--
-- Operational, NOT corpus: never embedded (peer to llm_call_log / alert /
-- agentlog). Dark: the writer is best-effort and fail-open — a ledger
-- write failure must never break the tool call it's measuring.
--
-- Kept lean deliberately (llm_call_log's lesson: this table takes a row
-- per tool call, not per LLM call — much higher write volume, so no index
-- beyond what the GROUP BY (verb, kind, error_type) + time-window mining
-- query and the retention GC actually need).

CREATE TABLE IF NOT EXISTS tool_calls (
    call_id      BIGSERIAL   PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    agentlog_id  BIGINT,                 -- correlates to kind='agentlog' ref; no FK (agentlog GC'd independently)
    source       TEXT,                   -- ambient cascade tier (opus/sonnet/haiku) from PRECIS_CURRENT_MODEL; NULL = interactive/direct
    profile      TEXT,                   -- PRECIS_MCP_PROFILE ('typed' | 'command') at call time
    verb         TEXT        NOT NULL,   -- get/search/put/edit/delete/tag/link/more
    kind         TEXT,                   -- the caller-supplied kind= (raw input, pre-resolution); NULL when omitted
    input_keys   JSONB,                  -- sorted array of top-level input kwarg NAMES ONLY (never values)
    outcome      TEXT        NOT NULL,   -- 'ok' | 'error'
    error_type   TEXT,                   -- PrecisError subclass name (BadInput, NotFound, ...) when outcome='error'
    result_count INT,                    -- reserved: populated once Response carries a structured hit count
    latency_ms   INT
);

-- Time-window bound (every mining query starts with "last N days").
CREATE INDEX IF NOT EXISTS tool_calls_ts_idx ON tool_calls (ts DESC);

-- The GROUP BY (verb, kind, error_type) confusion-mining query's natural
-- filter/group prefix; also serves per-verb/per-kind drill-down.
CREATE INDEX IF NOT EXISTS tool_calls_verb_kind_ts_idx
    ON tool_calls (verb, kind, ts DESC);

-- Partial: error-rate mining only touches the (small) error subset.
CREATE INDEX IF NOT EXISTS tool_calls_error_type_ts_idx
    ON tool_calls (error_type, ts DESC) WHERE error_type IS NOT NULL;

COMMENT ON TABLE tool_calls IS
    'Per-dispatch() telemetry: verb/kind/key-set/outcome. No payload content.';
COMMENT ON COLUMN tool_calls.input_keys IS
    'Top-level input kwarg NAMES only (JSONB array) — never values or bodies.';

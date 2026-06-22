-- 0034_agentlog_kind.sql
--
-- Register `kind='agentlog'` — the run-attribution record. Every time an
-- agent (a plan_tick coroutine, an operator-requested change, a chat
-- follow-up) touches the corpus, the run opens an `agentlog`: the full
-- assembled prompt it was given, the model/source, and — via the
-- `touched` link relation below — every chunk it wrote or moved. So a
-- chunk that "looks wrong" can be walked back to the exact run that
-- produced it and that run's transcript debugged.
--
-- Lineage: this is the read/write twin of `kind='alert'` (machine
-- telemetry, numeric-id, NOT embedded). Where an alert is a *condition*,
-- an agentlog is a *run*. Many agentlogs may touch one chunk (it gets
-- rewritten across runs); the link graph carries the many-to-many.
--
-- Shape (cf. 0029_alert_kind.sql):
--   * numeric-id, note-like — same family as alert / gripe / job.
--   * NOT embedded — no `card_combined`, the embed / chunk_keywords
--     workers never touch it. The headline lives in `refs.title`; the
--     prompt + model + source + transcript pointer live in `meta`.
--   * lifecycle is implicit: the sweeper GCs agentlogs (and their
--     `touched` links — never the chunks) past a retention window.
--
-- The `touched` relation is registered SYMMETRIC with no inverse
-- (like `related-to`): one row per (run, chunk) edge, surfaced from
-- either end by `links_for(direction='both')` and `chunk_connections`,
-- so the draft reader shows "📜 written by run N" with no row-doubling.
-- Keep this in sync with the `Relation` Literal in store/types.py.
--
-- Forward-only (ADR 0005). Idempotent under ON CONFLICT DO NOTHING.

BEGIN;

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('agentlog', TRUE, 'Agent log',
     'Run-attribution record — one per agentic run (plan_tick, operator '
     'change request, chat follow-up) that touches the corpus. Carries '
     'the full assembled prompt, model + source, and `touched` links to '
     'every chunk the run wrote or moved, so a suspicious chunk can be '
     'walked back to the run that produced it. Numeric id; deduped per '
     'run; GC''d past a retention window (links drop, chunks stay). '
     'Not embedded — surfaced by the /agentlogs web tab and chunk '
     'connections, not semantic search. See ``precis-agentlog-help``.')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('touched', TRUE, NULL,
     'Source agent run wrote or moved target chunk (run-attribution). '
     'Symmetric for graph purposes — surfaced from either end.')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0034_agentlog_kind.sql

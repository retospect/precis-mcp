-- ===========================================================================
-- precis v2 — migration 0003: Perplexity Sonar kinds
-- ===========================================================================
--
-- Phase 4b adds three Perplexity-backed kinds — websearch, think, research
-- — each mapped to a different Sonar model and cost tier. The original
-- 0001 schema folded them into a single `web` kind, but in practice the
-- three modes have wildly different cost profiles (~$0.001 vs ~$0.005 vs
-- ~$0.50 per call) and timeouts (30s vs 120s vs 600s) so the agent
-- benefits from picking the right one explicitly.
--
-- All three share the existing `perplexity` provider slug (added in
-- 0001). Cache keys disambiguate between models by including the model
-- name in the request hash, so two queries with the same prompt but
-- different models never collide.
--
-- Idempotent: ON CONFLICT DO NOTHING so re-runs are safe.
-- ===========================================================================

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('websearch', FALSE,
     'Web search',
     'Perplexity Sonar — fast factual web search (~$0.001/call, 2–5s)'),
    ('think',     FALSE,
     'Think',
     'Perplexity Sonar Reasoning Pro — analytical with reasoning traces (~$0.005/call, 5–30s)'),
    ('research',  FALSE,
     'Research',
     'Perplexity Sonar Deep Research — multi-step investigation (~$0.50/call, 2–10 min)')
ON CONFLICT (slug) DO NOTHING;

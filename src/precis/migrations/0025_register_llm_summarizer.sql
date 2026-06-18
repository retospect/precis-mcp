-- 0025_register_llm_summarizer — register the LLM summarizer tier.
--
-- The new `llm_summarize` worker pass writes a model-authored
-- "very brief; some additional detail" summary into
-- `chunk_summaries.text` under `summarizer = 'llm-v1'`. Because
-- `chunk_summaries.summarizer` carries a FK to `summarizers(name)`
-- (`chunk_summaries_summarizer_fkey`), the registry row must exist
-- before any such insert or the write is rejected.
--
-- This is a distinct artifact from:
--   * `rake-lemma`            — lexical keyword string (also in
--                               `chunk_summaries`, different row).
--   * `chunks.keywords` (F20) — per-chunk KeyBERT, on the chunk row.
--
-- Forward-only and idempotent (ON CONFLICT DO NOTHING). `is_default`
-- stays false — `rake-lemma` remains the default summarizer; the LLM
-- tier is opt-in (`precis worker --only llm_summarize`).
--
-- `config` records the serving target for provenance; bump the
-- `version` (and the worker's SUMMARIZER_NAME to `llm-v2`) to
-- re-summarize the corpus without destroying v1 rows.

INSERT INTO public.summarizers (name, config, is_default, description)
VALUES (
    'llm-v1',
    '{"endpoint": "litellm", "alias": "summarizer", "model": "qwen3-next-80b-a3b", "format": "brief;detail", "version": "1"}'::jsonb,
    false,
    'LLM brief+detail chunk summary (Qwen3-Next-80B-A3B via the litellm `summarizer` alias)'
)
ON CONFLICT (name) DO NOTHING;

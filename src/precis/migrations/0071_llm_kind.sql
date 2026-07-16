-- 0071_llm_kind.sql
--
-- The `llm` catalog kind — model choice as a queryable, learnable resource
-- (docs/proposals/llm-catalog.md). Turns model selection from hardcoded
-- constants (`router._TIER_MODEL` + the `LLM:opus|sonnet|haiku` tag) into a
-- first-class precis kind: a CATALOG of model facts + a LEDGER of observations
-- + a POLICY that picks. Slice 1 is the read-only catalog + a reconcile pass;
-- every layer degrades to today's behaviour when the catalog is empty (`Tier`
-- stays the floor), so it ships dark.
--
-- Numeric-id ref (like memory/quest/gripe): `title` = the capability prose
-- (embedded as the reused `card_combined` chunk, ord=-1, so an `llm` card IS a
-- vector — `search(kind='llm', q='careful SQL')` matches on capability).
-- `meta` carries the structured facts:
--   * `model_id`    — the canonical model slug (`claude-opus-4-8`), the human
--                     key `get(kind='llm', id='claude-opus-4-8')` resolves; also
--                     stamped as a `model:<slug>` tag so the lookup is a filter.
--   * `tier_floor`  — the `Tier` this model backstops (the degrade-to-floor).
--   * `offerings`   — operating points [{effort, transport, endpoint, max_input,
--                     max_output, price_in, price_out, quant}] — effort/window
--                     are axes WITHIN a card, not a row explosion.
--   * `capability`  — coarse 1–5 ordinal axes (code / long-context-recall /
--                     tool-structured / reasoning-convergence / summarize).
--   * `provenance`  — where each fact came from + when reconciled.
-- corpus_role is authored/'none' (never cited as evidence) — enforced by the
-- handler/export guard in code (KindSpec default), not here.
--
-- Also registers the `llm_review` chunk_kind: the append-only, typed, dated
-- review-log entry (the gripe body+comment / quest_log WORM pattern), written
-- via an `edit`-append in slice 3 (published-benchmark / measured-eval /
-- observed-telemetry / agent-review, each with a `by` + provenance). Registered
-- now so the chunk vocabulary is stable; slice 1 mints none.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('llm', TRUE, 'LLM catalog',
     'A model catalog card — one ref per model (claude-opus-4-8, qwen-heavy). '
     'Body is the capability prose (embedded, so the card is a vector); meta '
     'carries the structured facts (model_id, tier_floor, offerings, capability '
     'axes, provenance). A reconcile pass keeps the facts true against the live '
     'OpenRouter feed and flags drift. Read with get(kind=''llm'', '
     'id=''claude-opus-4-8'') or search(kind=''llm'', q=…). Never exported. '
     'See docs/proposals/llm-catalog.md.')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('llm_review', FALSE,
     'LLM catalog review-log entry — a WORM, dated, append-only ledger row '
     '(published-benchmark / measured-eval / observed-telemetry / agent-review) '
     'carrying entry_type + by + provenance in meta. The ledger layer of the '
     'catalog; the tote rolls up llm_call_log alongside it (slice 3).')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0071_llm_kind.sql

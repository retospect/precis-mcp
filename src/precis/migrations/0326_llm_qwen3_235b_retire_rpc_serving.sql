-- 0326_llm_qwen3_235b_retire_rpc_serving.sql
--
-- gr572844: the `llm` catalog card for `qwen3-235b-a22b-2507` still
-- advertises the distributed llama.cpp RPC serving path — a llama-server
-- main node plus a ggml-rpc worker node over the 200G link — in its
-- `title` (the embedded `card_combined` prose) and in
-- `meta.params.serving` ("distributed llama.cpp rpc", a server node plus
-- a worker node). That stack is gone: `deploy/` wires one
-- sglang-resident model per host (gpt-oss-120b, role
-- `deploy/roles/sglang`), the runbook "Distributed llama.cpp across the
-- DGX nodes" is marked retired, and the card carries no
-- `served_by`/`offerings` entries for it — nothing
-- routes there, but the prose still tells a router or agent that a
-- bookable local endpoint exists (the finding's stated risk).
-- qwen3-235b is not served by the sglang stack, so there is nothing to
-- repoint to: this retires the stale serving claims and re-describes the
-- card as the cloud-served (OpenRouter) model it actually is.
--
-- * `refs.title` — rewritten to honest card prose: the model, and that
--   booking goes through the reconcile-maintained OpenRouter variants in
--   `meta.endpoints`. The `llama-server` guard preserves any hand-edit
--   that already landed, so a replay is a no-op.
-- * `meta.params.serving` and `meta.params.quant` — both describe the
--   retired local GGUF form (Q6_K 4-shard); per-variant quant lives on
--   `meta.endpoints`. Removed, matching the curated {size, arch,
--   license} params shape the frontier seeds use.
-- * `chunks` ord -1 `card_combined` — re-emitted from the new title via
--   the declared DELETE + INSERT exception (never UPDATE — the fresh row
--   re-enters the embed queue; `ord < 0` card variants are the
--   append-only exception).
--
-- Left alone: `meta.endpoints` (live, reconcile-owned), `tier_floor`,
-- `suitability`, `provenance` (dated history), and the `llm_review`
-- chunk. Reconcile only writes facts_openrouter/endpoints/offerings, so
-- nothing re-mints the removed keys. Statements key on
-- `meta->>'model_id'` (0212's portable-identity rule), never ref_id.
-- Forward-only (ADR 0005). Squawk: UPDATE/DELETE/INSERT on live rows,
-- no DDL.

BEGIN;

-- 1. Honest card prose — fires only while the stale RPC claim is in the
--    title, so a replay (or an operator's own fix) is a no-op.
UPDATE refs
   SET title = 'Qwen3-235B-A22B-Instruct-2507 (Alibaba, Apache-2.0) — open-weight 235B MoE, 22B active, ~256K context, tools + structured output. Cloud-served only: the bookable provider×quant variants live in meta.endpoints and are kept current by llm_reconcile. The local llama.cpp RPC pair that served it was retired 2026-11 — the GB10 fleet now runs one sglang-resident model per host (gpt-oss-120b); this model is not served on the local stack.'
 WHERE kind = 'llm'
   AND retired_at IS NULL
   AND meta->>'model_id' = 'qwen3-235b-a22b-2507'
   AND title LIKE '%llama-server%';

-- 2. Drop the retired local-serving form facts from meta.params
--    (`serving` = the RPC pair claim, `quant` = the local GGUF file
--    form). The `?` guards make a replay a no-op.
UPDATE refs
   SET meta = meta #- '{params,serving}' #- '{params,quant}'
 WHERE kind = 'llm'
   AND retired_at IS NULL
   AND meta->>'model_id' = 'qwen3-235b-a22b-2507'
   AND (meta->'params' ? 'serving' OR meta->'params' ? 'quant');

-- 3. Re-emit the card_combined chunk so semantic search stops matching
--    the retired RPC prose — the upsert_card_combined pattern: DELETE
--    only when the stored text is stale, INSERT only when no card row is
--    left, so a replay writes nothing.
DELETE FROM chunks c
 USING refs r
 WHERE c.ref_id = r.ref_id AND c.ord = -1 AND c.chunk_kind = 'card_combined'
   AND r.kind = 'llm' AND r.retired_at IS NULL
   AND r.meta->>'model_id' = 'qwen3-235b-a22b-2507'
   AND c.text IS DISTINCT FROM r.title;

INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta)
SELECT r.ref_id, -1, 'card_combined', r.title, '{}'::jsonb
  FROM refs r
 WHERE r.kind = 'llm' AND r.retired_at IS NULL
   AND r.meta->>'model_id' = 'qwen3-235b-a22b-2507'
   AND NOT EXISTS (SELECT 1 FROM chunks c
                    WHERE c.ref_id = r.ref_id AND c.ord = -1
                      AND c.chunk_kind = 'card_combined');

COMMIT;

-- End of 0326_llm_qwen3_235b_retire_rpc_serving.sql

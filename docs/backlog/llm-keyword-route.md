---
status: in-progress
title: Keyword-triggered LLM model routing
pillar: local-compute
---

# Keyword-triggered LLM model routing

Reto 2026-10-23 (chat-99): "a switcher that changes llm models whenever a
keyword is present in the text. We keep a list of keywords to send to
specific models."

## Motivation / why

Routing today is *source-shaped*: tier defaults, `llm.op.<source>`
overrides, `llm.chain.<tier>` ladders. Nothing reads the prompt. Operators
want content-triggered routing — "when the text says `<keyword>`, send the
call to `<model>`" — without touching call sites. One ordered rule list,
evaluated at the one seam every routed call passes through, gets that
without per-caller code.

## In scope

- **`src/precis/utils/llm/keyword_route.py`** — new module owning the rule
  model, code defaults, the DB snapshot and matching:
  - `KeywordRule` (frozen): `rule_id`, `pattern`, `is_regex`, `target`
    (the `llm.op.*` override grammar: `{"tier"?, "model"?, "chain"?}`),
    `sources` / `tiers` scope (`None` = all), `enabled`, `note`.
  - `match(req_text, *, source, tier) -> KeywordRule | None` — ordered,
    first enabled+in-scope match wins; case-insensitive; plain keywords
    match **whole-word** (`re.escape` + `(?<!\w)`/`(?!\w)` boundaries);
    `is_regex` rules run their pattern (case-insensitive `search`).
  - Request text for matching = `req.prompt` + system prompt + user-message
    text (the same content `_route_features` counts).
  - **Snapshot cache, fail-open** — the `outbound_policy` pattern verbatim:
    one `SELECT` of the rule rows, refreshed at most every ~30 s per
    process; a DB fault serves the code defaults alone and never wedges a
    dispatch; `reset_cache()` for tests/write paths.
  - **Effective rule list** = DB rows (ordered by `position`), then code
    defaults (`CODE_RULES`, empty today) — the DB block wins by matching
    first, the same "code default, DB row wins" layering as the
    rate-limiter overrides.
- **Router seam** (`router.route`): after op resolution and the *normal*
  chain is resolved, a matched rule proposes a keyword plan — `target.tier`
  remaps the tier (same `_replace` as the op override), `target.chain`
  supplies the ladder (same rung grammar, `parse_chain_rungs` +
  `filter_tool_rungs` when `tools_needed`), `target.model` alone
  synthesizes a rung by inference (claude id → the tier's claude
  transport, `local_serving.served_locally` → `local`, else the hosted-OSS
  wire). The keyword plan is committed only after it passes the **same**
  gates the normal rung faces — `_drop_unavailable_rungs` host check, a
  non-tallying `gate_tier` probe, `check_dispatch` window admission. Any
  refusal ⇒ the miss is logged (`rule_id` + reason) and the call falls
  back to the already-resolved normal path. A committed keyword ladder
  then runs the identical downstream flow (real `gate_tier`, slot, retry,
  failover) — the limiter and `tools_needed` are never bypassed.
- **`dispatch_async` parity**: resolves the rule first; a match delegates
  to sync `route()` (a retargeted call gives up the `on_event` stream —
  correct by construction, streaming is a claude-agent nicety).
- **`req.model` pins are not routed**: a call-site model pin is a
  correctness constraint (the `classify:escalate` rule); a keyword rule
  never overrides it — the call routes normally.
- **Storage — `llm_keyword_routes` table** (forward-only migration; number
  comes from builder-ce, seeded empty):
  `id serial pk, position int, pattern text, is_regex bool, target jsonb,
  sources text[], tiers text[], enabled bool, note text, created/updated
  timestamptz`.
- **Edit surface** — a Keyword rules section on the `/llm` pages
  (`src/precis_web/routes/llm.py` + `templates/llm/`): list rules in order
  with enabled toggles, an add form (pattern, is_regex, target, scope,
  note), and up/down reorder; plus a **preview**
  (`GET /api/llm/keyword-route?text=&source=&tier=`) returning which rule
  fires and its resolved target. `precis` must never import `precis_web`;
  CLI/MCP read access is acceptable, MCP writes are not in scope.
- **Logging**: a routed keyword call stamps `features.keyword_rule_id` +
  `keyword_model` onto the `llm_call_log` row (JSONB — no column needed);
  a fall-back miss logs `rule_id` + reason at `warning`.
- **Regex safety**: rules are operator-entered, but a pathological pattern
  must not hang the router — pattern length capped (200 chars), compiled
  **once at snapshot load** (an invalid pattern disables that rule at load
  with a warning, never at match time), backreferences rejected, and
  plain keywords are the documented-preferred form (a literal match cannot
  backtrack). No `regex` third-party dep.
- **Performance**: matching runs on every dispatch — the compiled rule set
  is cached with the snapshot (no per-call DB, no per-call compile); empty
  rule set short-circuits to `None` before any scanning.

## Explicitly NOT in scope

- MCP/web-delete of rules (disable is the operator op; delete is SQL).
- Prompt-text mutation, prompt injection defense, or content policies.
- Routing non-`route()` paths (the `fix_gripe` router bypass).
- A per-rule hit counter / analytics beyond the route-log feature stamp.

## Acceptance criteria

- A rule (`pattern`, `target`) routes a matching call to the rule's model;
  first match wins; no-match resolves byte-for-byte today's decision.
- Whole-word + case-insensitive: `opus` matches "ask Opus" not "opuses".
- Scope `sources`/`tiers` and `enabled=false` rules are skipped.
- A target unavailable on the host, limiter-tripped, or window-refused
  falls back to normal routing and the miss is logged.
- `tools_needed=True` on a tool-less keyword chain still enforces
  `carries_tools` (emptied → normal routing).
- Every routed keyword call's `llm_call_log.features` carries the rule id.
- The `/llm` rules section lists/adds/disables/reorders, and the preview
  endpoint names the firing rule for a sample text.
- Empty ruleset ⇒ zero measurable dispatch overhead (cached, no DB hit).

## Target + blast radius

`src/precis/utils/llm/router.py` (`route`, `dispatch_async`,
`_route_features`), new `src/precis/utils/llm/keyword_route.py`, new
migration `NNNN_llm_keyword_routes.sql` (number via builder-ce),
`src/precis_web/routes/llm.py` + `src/precis_web/templates/llm/`,
`tests/utils/test_llm_keyword_route.py`, `tests/precis_web/` preview test.

## Open questions / decisions log

- **Keyword rules beat the op override when both apply** (content signal is
  the more specific operator intent; a miss/fallback re-runs the full
  normal path including `llm.op.*`).
- **Migration number**: not picked here — when the SQL is ready this tree
  signals `needs migration number from builder-ce` on its purpose line.

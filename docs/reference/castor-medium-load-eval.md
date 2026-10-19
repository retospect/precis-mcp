# Castor MEDIUM-tier load eval

Measurement for the castor-load decision (gr489474, Reto "option 1",
2026-10-18) — replay, no routing change. Harness:
`scripts/llm_eval/medium_castor_eval.py`; raw output is gitignored
(`scripts/llm_eval/gold_set/local/medium_castor_eval.jsonl`).

50 real MEDIUM calls replayed side-by-side: the prod cloud rung (claude_p →
haiku) vs the local rung (gpt-oss-120b on the served endpoint). judge =
local/cloud/tie/both-wrong wins on the frontier-judged sample. GPU-s = sum
of per-call wall times on the local arm (serial=True). Run 2026-10-18.

| site (route-log source) | n | agree% | judge L/C/tie/bad | cloud p50/p95 s | local p50/p95 s | cloud $/call | local GPU-s |
|---|---|---|---|---|---|---|---|
| chase:verify (chase:verify) | 24 | 88% (21/24) | 0/3/0/0 | 37.8/58.7 | 8.0/13.3 | $0.0346 | 206 (8.6/call) |
| reground-judge (taproot:reground-judge) | 14 | 86% (12/14) | 2/0/0/0 | 30.1/42.6 | 3.3/4.6 | $0.0303 | 48 (3.4/call) |
| negate (conflict_search:negate) | 8 | 12% (1/8) | 4/3/0/0 | 27.7/37.0 | 3.6/5.2 | $0.0270 | 31 (3.9/call) |
| locate (chase:locate) | 4 | 100% (4/4) | 0/0/0/0 | 23.2/25.1 | 5.7/6.9 | $0.0267 | 19 (4.8/call) |

## Notes

- Total paid-equivalent spend $3.612 (cap $15.0). Zero transport errors,
  zero empty replies on either arm (50/50 both sides).
- Cloud $/call is the claude -p OAuth envelope's reported cost_usd
  (subscription-equivalent, not metered API spend).
- Local arm = single-rung local chain + served_by endpoint via
  `LlmRequest.local_url` (no slot reservation — no prod writes); rung shape
  `{"placement":"local","model":"openai/gpt-oss-120b","transport":"local",
  "max_tokens":2400,"timeout_s":300,"reasoning_effort":"low"}`.
- Cloud arm = prod MEDIUM rung 0 only; the chain's hosted-OSS/claude_agent
  fallbacks are recorded as errors, not exercised.
- Agreement compares the site's decision field (`supports` / `verdict` /
  pick / paraphrase count). `negate`'s 12% is count-parity only — a
  different paraphrase count is not a quality signal; read its judge
  column instead.
- judge = claude-opus-5 blind A/B on 12 pairs, disagreements first
  (3 verify + 2 reground + 7 negate + 0 locate — locate had none).
- Sampling was stratified toward the minority verdicts, so agreement% is
  conservative vs the production mix (89% PRUNE / 89% "no").

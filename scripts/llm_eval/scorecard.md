# LLM eval scorecard — OSS vs claude (through the router switch)

15 models × 17 tasks, 608.0s. Ordinal 1–5 per axis (higher better); mean across axes. `$` = this run's OpenRouter spend (tiny prompts — a relative signal, not prod cost). ★ = incumbent for its tier.


## cloud-super

| model | code | reason | tool | recall | summ | mean | $ | errs |
|---|---|---|---|---|---|---|---|---|
| claude-opus-4.8 ★ | 5 | 5 | 5 | 5 | 2 | 4.4 | $0.0480 | 0 |
| deepseek-v4-pro (5/5≥★) | 5 | 5 | 5 | 5 | 2 | 4.4 | $0.0094 | 0 |
| glm-5.2 (4/5≥★) | 5 | 5 | 5 | 5 | 1 | 4.2 | $0.0098 | 0 |
| kimi-k3 (4/5≥★) | 5 | 5 | 5 | 5 | 1 | 4.2 | $0.0667 | 0 |

## cloud-mid

| model | code | reason | tool | recall | summ | mean | $ | errs |
|---|---|---|---|---|---|---|---|---|
| claude-sonnet-5 ★ | 5 | 5 | 5 | 5 | 1 | 4.2 | $0.0228 | 0 |
| minimax-m3 (5/5≥★) | 5 | 5 | 5 | 5 | 2 | 4.4 | $0.0053 | 0 |
| qwen3.7-max (5/5≥★) | 5 | 5 | 5 | 5 | 2 | 4.4 | $0.0620 | 0 |
| glm-4.7 (5/5≥★) | 5 | 5 | 5 | 5 | 2 | 4.4 | $0.0278 | 0 |
| kimi-k2.7-code (5/5≥★) | 5 | 5 | 5 | 5 | 1 | 4.2 | $0.0135 | 0 |

## cloud-small

| model | code | reason | tool | recall | summ | mean | $ | errs |
|---|---|---|---|---|---|---|---|---|
| claude-haiku-4.5 ★ | 4 | 4 | 5 | 5 | 2 | 4.0 | $0.0153 | 0 |
| deepseek-v4-flash (5/5≥★) | 5 | 5 | 5 | 5 | 5 | 5.0 | $0.0008 | 0 |
| gpt-oss-120b (5/5≥★) | 5 | 5 | 5 | 5 | 4 | 4.8 | $0.0015 | 0 |
| gpt-oss-20b (5/5≥★) | 5 | 5 | 5 | 5 | 4 | 4.8 | $0.0007 | 0 |
| qwen3.6-flash (5/5≥★) | 5 | 5 | 5 | 5 | 2 | 4.4 | $0.0604 | 0 |
| glm-4.7-flash (5/5≥★) | 5 | 4 | 5 | 5 | 2 | 4.2 | $0.0049 | 0 |

**Read it:** in each tier the OSS rows flagged `(k/5≥★)` match-or-beat the claude incumbent on k of 5 axes. A safe default swap needs 5/5 on the axes that tier actually runs (super/mid: code+reason+tool; small: tool+recall+summ).
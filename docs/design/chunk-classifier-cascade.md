# Chunk-tag classifier — the cascade (ADR 0047)

How the controlled chunk tags (ADR 0047) actually get written: a cheap
local model does the coarse, high-value calls for free; a stronger model
is reserved for the narrow, expensive residual. Grounded in a measured
eval, not vibes — see `scripts/classify/EVAL_RESULTS.md` for the numbers.

## Why a cascade

The rhetorical `role` axis has an irreducibly hard core — the
**attribution test** (is this the paper's OWN claim or a recap of
OTHERS'?), which is what citation-grounding depends on. Measured facts:

- The free local model (`summarizer` alias) does **72%** on the 11-way
  `role` — below the 85% gate. `related-work` recall is 10/39: it cannot
  reliably tell own-work from others'-work at fine grain.
- But it's **excellent at the coarse calls**: 94% discard-precision on
  junk (furniture vs substance), and **88% / 91%-own-precision** on the
  3-way collapse `own / background / furniture`.
- Human inter-annotator agreement on `role` is ~89% (blind audit) — so
  ~85–90% is the *ceiling*, not 99%. The residual is genuine ambiguity,
  absorbed by the gold `accept:` sets and, at query time, the agent.

So: don't force the fine axis on the cheap model. Make the cheap model do
what it's good at, and escalate only the hard bit.

## The three tiers

| tier | what | model | cost |
|---|---|---|---|
| **0** | regex/heuristics drop obvious furniture (references, copyright, ORCID, Elsevier, tiny) | none | free |
| **1** | `junk` gate → `role3` (own/background/furniture) | local `summarizer` | cheap |
| **2** | re-judge `own` chunks (attribution-critical) | stronger model | only the residual |

On prod (1.29M body chunks) Tier 0's regex already covers ~24% for free.
Tier 1 handles the rest at 88% / 91%-own-precision. Tier 2 is optional and
gated (`--escalate-model` / `PRECIS_CLASSIFY_ESCALATE_MODEL`); reserve it
for pushing own-precision past 91% when a use demands it. Escalating only
the ~15–25% ambiguous residual ≈ ~$200–400 vs ~$1.3–2.6k for all-strong.

## What gets written

One chunk tag per body chunk: **`ROLE3:own | background | furniture`**
(`Tag.closed("ROLE3", value)` → `chunk_tags`, `pos=ord`,
`replace_prefix=True` so it's single-valued). The junk gate folds into
`ROLE3:furniture` (no separate JUNK tag in the cascade). The 11-way
`role` axis remains available as an *optional refinement* of `own` chunks.

**Retrieval use.** `ROLE3:own` is the citation-grounding filter — 91%
precision means "cite this as the paper's own claim" is right 9/10 on a
free model; feed that candidate set to the agentic search to verify. Use
it as a soft boost / candidate filter, never a hard precision gate on its
own (see `EVAL_RESULTS.md` → recommendation).

## Idempotency, versioning, reversibility

- **Lease:** each chunk is claimed in `chunk_claims` under artifact
  `classify:cascade-v<CLASSIFY_VERSION>` (`FOR UPDATE SKIP LOCKED`), so
  parallel workers never double-classify.
- **Idempotent claim:** excludes chunks already carrying a `ROLE3` tag.
- **Re-tag:** bump `CLASSIFY_VERSION` (`workers/classify.py`) → new
  artifact → the corpus re-claims lazily (the `keywords_meta` pattern).
- **Reversible:** `DELETE FROM chunk_tags … WHERE namespace='ROLE3'` and
  `DELETE FROM chunk_claims WHERE artifact='classify:cascade-v1'`. Tags
  are `set_by='agent'`.

## How to run

**Manual / bounded (the eval + backfill tool)** —
`scripts/classify/_classify.py`, dry-run by default:

```sh
classify --cascade --limit 200                 # dry-run: distribution only
classify --cascade --limit 2000 --commit       # write ROLE3 tags
classify --cascade --commit --escalate-model claude-haiku-4-5   # +Tier 2
classify --axis role3 --limit 50               # single-axis (eval/debug)
```

**Continuous (the worker pass)** — `workers/classify.py`,
`run_classify_pass`, registered in `cli/worker.py`. **Default-OFF** (a
1.3M backfill is deliberate, like `llm_summarize`):

```sh
PRECIS_CLASSIFY_ENABLED=1 precis worker --profile system   # or --only classify
PRECIS_CLASSIFY_ESCALATE_MODEL=claude-haiku-4-5 …          # enable Tier 2
```

Forces `model=summarizer` (the node's `PRECIS_SUMMARIZE_MODEL=qwen` is a
thinking model that returns empty). Node target: melchior (litellm proxy
+ the cheap alias). See [[gold-set-classify]] memory for the ops quirks.

## Files

| file | role |
|---|---|
| `src/precis/data/axes/{role3,junk,role,open-question}.yaml` | axis defs + prompts + few-shot |
| `src/precis/workers/classify.py` | the production ref-pass (`run_classify_pass`) |
| `src/precis/cli/worker.py` | registration (gated `PRECIS_CLASSIFY_ENABLED`) |
| `scripts/classify/_classify.py` / `classify` | manual cascade/backfill (dry-run default) |
| `scripts/classify/_eval*.py` / `eval-classifier` | accuracy harness (strict + accept-aware) |
| `scripts/classify/gold_set/` | 200-chunk + 30-paper gold, `RESOLVED.md` |
| `scripts/classify/EVAL_RESULTS.md` | the measured numbers + recommendation |

## Status

Built + measured + dry-run-and-bounded-commit-proven against prod. The
worker pass is wired but **default-OFF**; a full-corpus run and shipping
to main are deliberate human decisions. Lexical ref axes (`material`,
`transport`) and the `junk`+`role3` cascade are shippable on the free
model now; a stronger Tier 2 is an optional upgrade, not a prerequisite.

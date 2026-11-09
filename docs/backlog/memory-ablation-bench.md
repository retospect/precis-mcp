---
status: draft
pillar: memory-graph
title: Memory-ablation benchmark — a public one-command harness that measures what each memory layer buys in tokens and correctness, and the paper that reports it
prio: high
quest: qu604835
---

# Memory-ablation benchmark and paper

Reto, 2026-11-09 (chat): "PRecis memory has many aspects, kwd, summary,
categorizers, fisheye, autorag, rag. How do we show in a paper what
contributes to low token efficient operation or other aspects?" Then:
"it'd be nice if the benchmark could be auto-run by anyone" and
"reproducible science". Quest qu604835 (inquiry body) holds the
striving, the logbook and the lit-search memory; strategic root td604839
holds the work.

## What

One command, from a fresh clone on one machine, no cluster access:

```
scripts/bench memory-ablation            # full matrix → bench/results/<date>-<sha>.json
scripts/bench memory-ablation --quick    # scripted agent only, one seed
scripts/bench report bench/results/…json # tables + figures the paper embeds
```

It ingests a committed open-access fixture corpus into a fresh local DB,
runs a fixed task set under every ablation, and writes one results file
whose header pins everything a stranger needs to reproduce the numbers.

## Ablation matrix

Server-side flags (env or `service_config`), default all-on; each run
turns one off, plus two baselines:

| arm | what is removed |
|---|---|
| `no-kwd` | chunk keywords (the `kwd` rung and keyword bookmarks) |
| `no-summary` | gloss rung (`summary`) |
| `no-categorizer` | categorizer tag axes and taxon matches in ranking |
| `no-fisheye` | spatial neighbourhood — `fisheye*` collapses to `verbatim` |
| `no-recall` | `+recall` / auto-injected neighbourhood ("autorag", Reto 2026-11-09) |
| `lexical-only` / `semantic-only` | one side of hybrid fusion |
| baseline `chunk-rag` | naive top-k chunk retrieval, no graph |
| baseline `whole-paper` | read the paper(s) the gold chunk sits in |

## Agent under test, two tiers

1. **Scripted caller** — a deterministic verb sequence per task type
   (search → get at the rung the arm allows → stop at first gold or
   budget). Exact byte counts, no model cost; runs in CI.
2. **LLM agent** — a pinned model driving the same verbs over MCP with a
   call budget; tokens from the API usage field. Runs on demand; the paper
   reports both.

## Metrics per task

tokens returned (bytes/4 and, for the LLM arm, API input tokens) ·
calls to a correct answer · wall time · correctness (exact for `lookup`,
LLM judge with a pinned model for `cross-paper`/`stance`/`grounding`).
Aggregates follow `mesh_eval/retrieval.py`: mean recall@10 with a Wilson
CI, median tokens-to-first-gold, not-reached count; plus a recall-vs-tokens
curve per arm.

## Reproducibility

- **Fixture corpus**: a manifest of CC-BY arXiv papers (ids, sha256,
  licence), fetched by the harness; never prod text. The agent-memory
  papers already serving qu604835 are the seed.
- **Task set + gold**: committed JSONL in the `retrieval_qu164903.jsonl`
  shape (`{id, type, question, answer, evidence, gold_chunks, labelled_by}`);
  every label `reto` or marked `model:<id>`.
- **Results header**: precis sha, corpus hash, embedder id, model ids,
  judge model, seeds, flag set, machine class — eval-run-spine.md's run
  versioning with a first consumer.
- **CI**: nightly `--quick` run publishes the results artifact; a drift
  alert when a metric moves more than its CI against last week.
- **Archive**: corpus manifest + results + figure script get a Zenodo
  DOI per paper revision (catpath-repro-bundle.md precedent).

## Builds on (cite, do not duplicate)

- `src/precis/mesh_eval/retrieval.py` — recall@10, tokens-to-first-gold,
  search vs fisheye arms, keep rule; the harness is this module with
  flags, baselines and a public corpus.
- `scripts/llm_eval/gold_set/README.md` — gold format and review rule.
- `eval-run-spine.md` — run versioning, frozen eval world (here: a fresh
  local DB, cheaper than a prod restore).
- `graph-gardener-value.md` — the argument for what a stored edge buys
  that retrieval cannot recompute; this item measures the complement.
- `memory-retrieval-learning.md` — the recall-vs-tokens curve as the
  judged quantity.

## Slices (todo tree under td604839)

1. Per-call response size + token estimate in the tool-call log and
   `tool_calls` (`precis.tools.core._log_tool_call`); the one missing
   instrument. Ships first, independent of the rest.
2. Ablation flags on the server, default all-on, read once per call.
3. Fixture corpus manifest + `ingest-local` into a fresh DB.
4. Task set + gold, Reto-reviewed.
5. Harness + results schema + report script (tables, figures).
6. CI nightly + Zenodo bundle.
7. Paper draft (precis `draft`, serves qu604835): related work from the
   quest's inquiry ticks, results sections regenerated from slice 5.

## Explicitly NOT in scope

Changing any memory layer's behaviour to win the benchmark; a reranker
(memory-retrieval-learning.md owns that); prod-restore eval worlds;
benchmarking other vendors' systems beyond the two baselines.

## Target + blast radius

New `src/precis/bench/` and `scripts/bench`; a small read-only addition
in `src/precis/tools/core.py` (slice 1); `src/precis/mesh_eval/` gains
arms; `.github/` one nightly job; `bench/corpus/manifest.json`,
`bench/tasks/*.jsonl`. No migration unless slice 1 adds a column to
`tool_calls`.

## Open questions (Reto)

- Fixture corpus size: ~30 papers (CI-fast) or ~200 (closer to a real
  quest)? Default taken: 30 for `--quick`, 200 for the full run.
- Judge model for correctness on non-lookup tasks: pin one cloud model,
  or the local castor chain so a stranger pays nothing?

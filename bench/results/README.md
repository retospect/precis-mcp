# Memory-ablation results

`scripts/bench run --set quick` writes one file per run here,
`<YYYYMMDD>Z-<shortsha>-<set>.json` (UTC date, short precis git sha). The
files are gitignored; this README is the committed description of their
schema. `scripts/bench report <file>` prints the markdown tables.

Design and slices: `docs/backlog/memory-ablation-bench.md` (§Ablation matrix,
§Metrics per task, §Reproducibility). Code: `src/precis/bench/runner.py`
(runner, report), `layers.py` (coverage guard, local fill), `planted_ingest.py`
(planted facts into the dev DB).

## Reproduce

```
scripts/bench ingest --set quick      # QASPER fixture -> dev DB (pinned archive)
scripts/bench fill --embedder bge-m3  # embeddings + keywords (free, local; once)
scripts/bench run --set quick         # the scripted tier, all arms
scripts/bench report bench/results/<file>.json
```

`run` also ingests the planted-fact documents and memory nodes (idempotent) and
fills their embeddings/keywords, so `fill` beforehand only saves wall time.
Nothing here calls a paid API. Summaries, categorizer tags and machine edges
need an LLM; the dev DB does not have them, and the header says so.

## File shape

```
{ "header": {...}, "rows": [ {...}, ... ] }
```

### header

| key | meaning |
|---|---|
| `schema` | results schema version (1) |
| `tier` | `scripted` (the model-free caller; LLM tier is a later cut) |
| `set`, `corpus_sha256` | fixture set and its hash from `bench/corpus/manifest.json` |
| `tasks_file`, `tasks_sha256`, `tasks` | QASPER task file, its sha256, and counts (`qasper`, `planted`, `abstention`, `skipped`) |
| `planted` | `seed`, fixed `at`, counts, and `sha256` of the generated tasks + docs + memory nodes |
| `precis_sha`, `git_dirty` | code under test (`git_dirty`: `true` if the tree had uncommitted paths) |
| `embedder` | `name`, `model`, `backend`, `dim`, `meaningful` (`false` for the hash/mock embedder), `stored` (embedder -> chunk count in the DB) |
| `models`, `judge` | `null` on the scripted tier |
| `token_estimator`, `token_budget` | `chars/4` of the rendered dispatch body; per-task budget |
| `caller` | search shape, `read_k`, the extent `ladder`, `recall_ks`, stop rule |
| `arms` | per arm: `switches_off`, `vacuous` + `reason`, `degenerate`, `embedder_note`, `identical_to_all_on_frac` |
| `layer_coverage` | what the DB holds of each layer (see below) |
| `fill` | claimed/ok/failed counts of the embed and keyword passes run by this invocation |
| `machine`, `started_at`, `ended_at` | host python/platform; UTC `Z` timestamps |

### layer coverage and vacuous arms

`layer_coverage` is measured over the benchmark's own refs (the set's QASPER
papers plus the planted documents) before any arm runs: `chunks_with_keywords_frac`,
`chunks_with_llm_summary_frac`, `chunks_with_embedding_frac`,
`refs_with_categorizer_tag_frac`, `refs_with_edge_frac`, `mesh_edges`,
`embedder_counts`. An arm whose layer is present on less than 1% of its
denominator has nothing to remove: its `arms.<arm>.vacuous` is `true` and the
run prints a warning. What can still differ is boilerplate (for example the
"no structural neighbours at 1 hop" line `no-fisheye` drops), which
`identical_to_all_on_frac` quantifies. An arm is also marked vacuous when every
task's rendered transcript is byte-identical to `all-on`'s
(`identical_to_all_on_frac == 1.0`). `semantic-only` with no embeddings is
`degenerate` (no leg to rank with), not vacuous. Any arm that ranks with the
semantic leg carries `embedder: hash (not meaningful)` when the embedder is the
hash/mock one. A vacuous arm's delta is not a finding; do not report it as a null
effect.

### rows (the long table: one per task x arm)

| key | meaning |
|---|---|
| `task`, `type`, `source` | task id, gold type (`lookup`, `grounding`, `k-hop`, `stance`, `memory`), `qasper` or `planted` |
| `arm` | `all-on`, `no-kwd`, `no-summary`, `no-categorizer`, `no-fisheye`, `no-recall`, `lexical-only`, `semantic-only`, `chunk-rag`, `whole-paper` |
| `abstention` | unanswerable task: no gold, `found` and `tokens_to_first_gold` are `null`, only `tokens_total`/`calls` are meaningful |
| `applicable` | `false` where the arm cannot run the task (`whole-paper` on a `memory` task, which has no document) |
| `found`, `censored`, `stop` | gold rendered within the budget; `censored` = not reached, `stop` is `found`, `budget`, `exhausted` or `n/a` |
| `tokens_to_first_gold` | estimated tokens rendered up to and including the call that first rendered a gold-overlapping chunk; a censored task carries the budget; `whole-paper` is the full document cost (never censored) |
| `tokens_total`, `calls` | tokens and dispatch calls the whole sequence rendered/issued |
| `errors`, `refusals` | failed calls; and calls the server refused because the arm switched that rung/filter off (`BadInput` naming `PRECIS_BENCH_SWITCHES`) |
| `recall_at_{1,3,5,10}` | share of the task's gold items touched by a chunk among the top k search hits; `null` without gold and for `whole-paper` |
| `transcript_sha256` | hash of the normalised call/output stream (drives the vacuity check); `null` for the baselines |
| `wall_s` | wall time; the only non-deterministic field |

## What counts as found

The scripted caller (`search` -> `get` up the ladder `kwd`, `summary`,
`verbatim`, `fisheye`, `+recall` for each of the top 5 hits) stops at the first
call whose output renders a chunk whose **whole text** appears in the output and
whose character offsets overlap a gold span
(`precis.bench.gold.spans_overlap_chunk`, document-aware). A truncated preview
does not count. It always climbs the whole ladder: it has no relevance judgement
with which to skip a rung, so an arm that refuses a rung is cheaper by the
refused call, not by a better decision. A caller that decides which rung to
spend is the LLM tier. Planted `memory` tasks are found when the output contains
the planted fact sentence. Re-running with unchanged code, data and seed
reproduces every field except `wall_s`.

`chunk-rag` is plain top-10 chunk retrieval (one call, full chunk text, no
graph); `whole-paper` renders the whole canonical text of the task's documents.

# Memory-ablation results

`scripts/bench run --set quick` writes one file per run here,
`<YYYYMMDD>Z-<shortsha>-<set>-<caller>.json` (UTC date, short precis git sha,
caller `triage`, `rowkw` or `ladder`). The files are gitignored; this README is the
committed description of their schema. `scripts/bench report <file> [<file>…]`
prints the markdown tables, and with several files (one per caller) a side by
side comparison.

Design and slices: `docs/backlog/memory-ablation-bench.md` (§Ablation matrix,
§Metrics per task, §Reproducibility). Code: `src/precis/bench/runner.py`
(runner, report), `layers.py` (coverage guard, local fill), `planted_ingest.py`
(planted facts into the dev DB).

## Reproduce

```
scripts/bench ingest --set quick      # QASPER fixture -> dev DB (pinned archive)
scripts/bench fill --embedder bge-m3  # embeddings + keywords (free, local; once)
scripts/bench run --set quick         # the scripted tier, all arms, caller triage
scripts/bench run --set quick --caller ladder   # the old whole-ladder caller, for comparison
scripts/bench report bench/results/<triage>.json bench/results/<ladder>.json
```

`scripts/test` tears down this worktree's compose project, dev DB included (the
cause of gr613540's empty `precis_dev`). `pg_dump` the DB to `.scratch/` before
a run, do not run `scripts/test` or `scripts/ship` while a run is in flight, and
restore from the dump rather than re-embedding if the DB is empty.

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
| `caller` | `name` (`triage`, `rowkw` or `ladder`), search shape, `read_k`, the extent `ladder`, for `triage` and `rowkw` the `threshold` and `score`, `recall_ks`, stop rule |
| `arms` | per arm: `switches_off`, `vacuous` + `reason`, `degenerate`, `embedder_note`, `identical_to_all_on_frac`, `known_defect` (a defect of the code under test that bends this arm, with its gripe) |
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
| `arm` | `all-on`, `no-kwd`, `no-summary`, `no-categorizer`, `no-fisheye`, `no-recall`, `lexical-only`, `semantic-only`, `no-cheap-rungs` (`kwd` and `summary` both off; the full factorial is td606577), `chunk-rag`, `whole-paper` |
| `caller` | `triage`, `rowkw` or `ladder` for the scripted arms; `null` for the two baselines (they have no caller) |
| `abstention` | unanswerable task: no gold, `found` and `tokens_to_first_gold` are `null`, only `tokens_total`/`calls` are meaningful |
| `applicable` | `false` where the arm cannot run the task (`whole-paper` on a `memory` task, which has no document) |
| `found`, `censored`, `stop` | gold rendered within the budget; `censored` = not reached, `stop` is `found`, `budget`, `exhausted` or `n/a` |
| `tokens_to_first_gold` | estimated tokens rendered up to and including the call that first rendered a gold-overlapping chunk; a censored task carries the budget; `whole-paper` is the full document cost (never censored) |
| `tokens_total`, `calls` | tokens and dispatch calls the whole sequence rendered/issued |
| `verbatim_opens` | `get(extent='verbatim')` calls issued (chunks read whole) |
| `errors`, `refusals` | failed calls; and calls the server refused because the arm switched that rung/filter off (`BadInput` naming `PRECIS_BENCH_SWITCHES`). `triage` never asks for a removed rung, so its `refusals` is 0 for every arm; `ladder` counts them |
| `recall_at_{1,3,5,10}` | share of the task's gold items touched by a chunk among the top k search hits; `null` without gold and for `whole-paper` |
| `transcript_sha256` | hash of the normalised call/output stream (drives the vacuity check); `null` for the baselines |
| `wall_s` | wall time; the only non-deterministic field |

## Report

`scripts/bench report` prints, per arm over the answerable tasks: `n`, mean
`recall@5`, the **median tokens to first gold over the tasks that reached it**
(a censored task carries the budget in the file; averaging it in would make the
median the budget whenever over half are censored), the `censored` count, mean
`calls`, `refusals`, and the deltas against `all-on`. A delta between arms with
different `censored` counts compares different task subsets; read the two
columns together. Tables follow for the QASPER and planted tasks separately,
then the unanswerable tasks (mean tokens and calls only). A vacuous arm is
flagged `VACUOUS` and its delta is not a layer effect.

## What counts as found

All scripted callers stop at the first call whose output renders a chunk whose
**whole text** appears in the output and whose character offsets overlap a gold
span (`precis.bench.gold.spans_overlap_chunk`, document-aware), or at the token
budget. A truncated preview does not count. Planted `memory` tasks are found when
the output contains the planted fact sentence. Re-running with unchanged code,
data and seed reproduces every field except `wall_s`.

### The callers

`triage` (default): `search` the question (top 10). Read the cheapest rung the
arm has for every hit (`kwd`, else `summary`, else none) and score the hit by the
share of the question's content words found in that rung's rendered text (the
planted generator's tokeniser and stopwords). Open `verbatim` for hits whose
score is at least the threshold, best score first, ties in search rank, at most 5
opens. With no cheap rung every hit scores alike and is opened in search rank
order, which is what having no cheap rung costs. If the gold is still not
rendered: `fisheye` once on the best opened hit if that rung is on, then
`+recall` once on it if on. It asks `switches.current()` first and never
requests a removed rung, so `refusals` is 0 for every arm; a removed rung is
paid for as a missing signal (the hit goes unscored) and not as a cheaper
refused call.

`ladder` (the first cut): for each of the top 5 hits, `kwd`, `summary`,
`verbatim`, `fisheye`, `+recall` in turn, whatever it saw. It has no relevance
judgement with which to skip a rung, so an arm that refuses a rung is cheaper by
the refused call and a cheap rung can never show value. Kept only to compare.

`rowkw`: `triage` that scores each hit by the chunk keywords its search row
already prints (`pc9<TAB>kw, kw, ...<TAB>size`), not by a cheap-rung read, so the
signal costs nothing; then the same rule: open `verbatim` at or above its own
threshold (`runner.ROWKW_THRESHOLD`), best score first, at most 5, then
`fisheye` and `+recall` once on the best opened hit. With the keyword layer off
the rows carry no keywords: no signal, search rank order, no cut. This is the
caller closest to what an agent sees. Under `rowkw`, `no-kwd` and
`no-cheap-rungs` both remove the row keywords (the `kwd` switch cuts them),
so they are the same arm; `no-summary` changes nothing.

`no-cheap-rungs` is the honest cost of having neither cheap rung: under `triage`
there is no cheap read and the hits are opened in search rank order.

### The triage threshold

`runner.TRIAGE_THRESHOLD` is one constant for every arm, chosen by
`scripts/bench sweep` on the planted dev seeds 1, 2, 3 (3 x 40 tasks; seed 7 is
the reported run and QASPER is never used). The sweep plants the three seeds into a throwaway DB (`precis_dev_sweep`,
a restore of the run's DB so the reported run's DB gets no extra distractors),
runs `triage` at each threshold and picks the one with the lowest mean tokens to
gold over the two arms swept (`all-on`, whose cheap rung is `kwd`, and `no-kwd`,
whose cheap rung is `summary`), a task that did not reach the gold counting as the
budget, ties to the lower threshold. Run 2026-11-09 (bge-m3, 12000 token budget):

| arm | threshold | n | found | mean tok→gold (miss = budget) | mean opens | mean calls |
|---|---:|---:|---:|---:|---:|---:|
| all-on | 0 | 120 | 0.967 | 1149 | 1.04 | 10.3 |
| all-on | 0.1 – 0.6 | 120 | 0.150 | 10232 | 0.00 | 9.2 |
| no-kwd | 0 | 120 | 0.917 | 5439 | 1.52 | 10.9 |
| no-kwd | 0.1 | 120 | 0.617 | 7207 | 1.04 | 10.6 |
| no-kwd | 0.2 | 120 | 0.567 | 7531 | 0.97 | 10.6 |
| no-kwd | 0.25 | 120 | 0.508 | 7944 | 0.86 | 10.4 |
| no-kwd | 0.3 | 120 | 0.392 | 8667 | 0.59 | 10.1 |
| no-kwd | 0.4 | 120 | 0.342 | 9015 | 0.53 | 10.0 |
| no-kwd | 0.5 | 120 | 0.333 | 9067 | 0.47 | 9.9 |
| no-kwd | 0.6 | 120 | 0.267 | 9493 | 0.27 | 9.6 |

The pick is 0: no cut. The reason is in the rungs, not the policy. The `kwd` rung
renders only the paper's header line (`pa1 [paper] <title>`, about 90
characters), so for the planted documents (titled "Planted document NN") every
hit scores 0 and any positive threshold opens nothing (found 0.15 is the memory
tasks, which the search body already renders). The `summary` rung does carry the
hit's and its neighbours' keywords and does score above 0, but at every positive
threshold it throws away more gold than it saves tokens on paraphrased
questions. So with the layers as the dev DB holds them, the cheap rungs give
`triage` no usable signal: at threshold 0 it opens the hits in search rank order
and a cheap rung's cost is pure overhead. That is a finding about these rungs on
this corpus; a policy that scores something richer (the chunk keywords already
printed in the search rows, a relative cut against the best score) is a
different caller.

`rowkw` is swept the same way (`scripts/bench sweep --caller rowkw`, `all-on`
only: it reads no rung). Run 2026-11-10, planted dev seeds 1, 2, 3, 120 tasks:

| threshold | found | mean tok→gold (miss = budget) | mean opens | mean calls |
|---:|---:|---:|---:|---:|
| 0 | 0.942 | 1402 | 1.31 | 2.4 |
| 0.1 | 0.683 | 4231 | 0.88 | 2.1 |
| 0.2 | 0.625 | 4887 | 0.78 | 2.0 |
| 0.25 | 0.567 | 5546 | 0.68 | 1.8 |
| 0.3 | 0.425 | 7143 | 0.46 | 1.6 |
| 0.4 | 0.358 | 7895 | 0.37 | 1.4 |
| 0.5 | 0.350 | 7976 | 0.30 | 1.4 |
| 0.6 | 0.233 | 9292 | 0.17 | 1.2 |

The pick is again 0. Row keywords carry real signal (unlike the `kwd` rung) but
every cut loses more gold than it saves: a paraphrased question shares few
content words with an extractive keyword list. Even at 0 the best-first order
costs a little against search rank order (found 0.942 against `triage`'s
0.967 at 0): the keyword score re-ranks hits the retriever had right.

### What the `kwd` rung renders for a paper chunk

`get(id='pc…', extent='kwd')` renders one line, the owning paper's header
(`· pa1 [paper] <title> [created …]`, about 90 characters), and none of the
chunk's keywords. That is the code's intent for doc kinds, not a dropped field:
`eye_render._render_doc_eye` returns `f"· {head}"` for any `ext <= Extent.TOC`
before it reads the chunk list, and `working_set.Extent.TOC` is documented as
the "one-line bookmark, ancestor path un-collapsed" (`"kwd"`). For a chunk the
ancestor path is the ref. The chunk's keywords first appear at `summary`
(the `▸ pc… ~N [paragraph] kw, kw, …` focus line of `_fisheye_split`) and, for
every hit, in the `search` rows. So `kwd` is a ref-level rung when applied to a
chunk handle, which is why `triage` finds no signal in it.

### `lexical-only` is hybrid's lexical leg

Checked 2026-11-09 against `store.chunks`: hybrid's lexical leg
(`search_chunks_fused`, CTE `lex`) and `mode='lexical'`
(`search_chunks_lexical`) build the same query, `websearch_to_tsquery('english',
q)` (every term ANDed, stopwords dropped), rank by `ts_rank_cd DESC, chunk_id`,
and take the same filters. So the arm removes the semantic leg and nothing else.
The low `lexical-only` recall is that leg alone: on the first 25 QASPER questions
the AND query matches nothing at all for 11 of 25 (14 had at least one match), because a natural-language question
needs every content word in one chunk. In hybrid that leg contributes only where
it matches and the semantic leg ranks the rest. The test
`test_lexical_only_is_the_lexical_leg_of_hybrid_and_nothing_else` pins it. Two
differences exist and neither reaches the bench DB: the fused leg rewrites the
query through the canon taxonomy (`canon.lexical_q`) when taxa match, and
`mode='lexical'` does not; a single-paper `scope=` adds an OR retry that the
bench never uses.

`chunk-rag` is plain top-10 chunk retrieval (one call, full chunk text, no
graph); `whole-paper` renders the whole canonical text of the task's documents.

---
status: draft
pillar: memory-graph
title: Memory-ablation benchmark — a public one-command harness that measures what each memory layer buys in tokens and correctness, and the paper that reports it
prio: high
quest: qu604835
---

# Memory-ablation benchmark and paper

## The question (settled with Reto, 2026-11-09)

An agent pays for every byte a tool returns. A memory over documents adds
layers that each claim to cut that cost or raise answer quality —
keywords, summaries, categorizer tags, typed graph edges, the fisheye
neighbourhood, automatic recall, hybrid search, chunking — and the
interface adds more: tool descriptions, hints, row rendering, title
truncation, how trivially the agent can step to the previous or next
chunk. Precis has all of these, built on intuition. The literature
compares memory *systems* (LongMemEval, LoCoMo, arXiv 2603.04814);
vendors report their own tokens-per-call; nobody has measured the
*layers*, and none of it is re-runnable from a public repo.

**Primary:** for an agent answering questions over a paper corpus through
a tool interface, which memory layers reduce tokens and calls to a
correct answer, by how much, and how do they interact? Factorial ablation
on QASPER with its own gold.

**Secondary, same harness:** (a) surface — do description accuracy,
hints, row representation, truncation-robust titles and prev/next
navigation change error rate and path length? (b) model — does a weaker
model need a simpler API or only simpler skills? (c) chunking and scale —
which chunker and splitter, and does the benefit survive corpus growth?
(d) edges — do machine-written graph edges add anything a retrieval call
would not find?

**Contribution:** the first per-layer ablation of an agent memory, a
one-command benchmark a stranger can re-run on a fixed CC-BY corpus, and
design guidance on which layers earn their tokens, which are dead weight,
and for whom.

Reto, 2026-11-09 (chat): "PRecis memory has many aspects, kwd, summary,
categorizers, fisheye, autorag, rag. How do we show in a paper what
contributes to low token efficient operation or other aspects?" Then:
"it'd be nice if the benchmark could be auto-run by anyone" and
"reproducible science". Quest qu604835 (inquiry body) holds the
striving, the logbook and the lit-search memory; strategic root td604839
holds the work.

## What we measure, and against what (Reto, 2026-11-09, fifth ask)

Two quantities per task: **cost to reach the right evidence** (tokens
returned, calls) and **answer correctness**. "Right evidence" needs a
reference, so yes, gold — but gold from three sources, each answering a
different question, and the cheapest one carries the factorial:

| source | gold comes from | measures | role |
|---|---|---|---|
| **planted facts** (synthetic) | construction: we write a fact, plant it, and know where it is | the memory's write→recall function in isolation: cost to recollect a known bit; k-hop composition of planted facts; planted contradictions for stance | **primary for the factorial** — thousands of tasks, exact gold, no annotators, no OCR in the loop |
| **QASPER** (human) | 5,049 questions by people who saw only title+abstract; evidence marked by others | external validity on real documents and natural questions | the anchor that lets a reviewer compare to published numbers |
| **round-trip** (model) | a model reads a passage and writes the question | extends task types neither above covers (cross-paper) | flagged self-authored; smallest share |

Planted facts are "feed in data bits, pull back recollections": facts
go in both as memory nodes and as sentences injected into fixture
paragraphs; queries are generated from the *fact* before planting at a
controlled paraphrase level, lexical overlap between query and planted
text is recorded, and results are binned by it — so the vocabulary-leak
upper bound the `memory_recall` set suffers from becomes a measured
axis instead of a hidden bias. Difficulty is a dial: distance of the
plant from the paper's topic, number of hops, number of near-duplicate
decoys.

**OCR is not in this loop.** QASPER ships clean full text, and planted
facts are text by construction; ingest fidelity (PDF → marker) is the
`ingest-and-fetch` thread's axis. One optional arm, `input-pdf-ocr` (the
same QASPER papers through the PDF path), would price OCR noise against
the memory layers — out of the first paper unless cheap.

**Correctness reference**: planted → exact match on the fact; QASPER →
answer F1 against its answers and evidence recall against its spans;
round-trip → pinned judge. The factorial's dependent variable is
**tokens to a correct answer** (not reached = censored at the budget).

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

## Surface ablation matrix (Reto, 2026-11-09, second ask)

The memory layers above change *what is stored*; these change *how the
agent sees it*. Only the LLM-agent tier can be misled, so every arm here
runs on that tier; the scripted caller is the control (its numbers must
not move).

| arm | what changes | what it answers |
|---|---|---|
| `desc-terse` / `desc-stale` | tool descriptions cut to one line / left one release behind (a renamed arg, a dropped view) | does description accuracy change the error rate (`BadInput`/`Unsupported` per call) and calls-to-correct-answer? |
| `no-hints` | `Next:` tables and error-ack `next:` lines stripped from every result | do hints shorten the path? measured as hint uptake (next call equals a suggested call) and calls-from-error-to-success |
| `row-title` / `row-summary` / `row-kwd` / `row-all` | search hit rows carry the title only, the gloss, the chunk keyword extract, or all three | which index representation gets the gold row opened first, at what result-page size? |
| `title-topic` / `title-cut40` / `title-cut80` | fixture titles rewritten topic-first (against precis-firstline-help), or truncated at 40/80 chars in listings | are conclusion-first, truncation-robust first lines ("matryoshka" titles) worth their authoring cost? pick accuracy from the truncated listing |
| `nav-none` / `nav-handles` / `nav-hinted` | stepping to the previous/next chunk: no affordance (agent must re-search), `~ord±1` addressing only, or an explicit prev/next line in every chunk result | how trivially can the agent read on? calls and tokens from a hit to its adjacent context; share of grounding answers that needed the neighbour |

Extra metrics for this matrix: error rate per verb × kind, hint uptake
rate, first-pick accuracy (gold row opened on the first `get`), result
page bytes.

**Free first result, before any harness code:** prod's tool-call log
already records every failed call at WARNING with the full payload
(`precis.tools.core._log_tool_call`), so the current error rate per
verb × kind, the top misuse shapes, and the share of errors followed
by the ack's `next:` call are one read-only mining pass — the
observational baseline the `desc-*` and `no-hints` arms are judged
against.

## Model × surface matrix (Reto, 2026-11-09, third ask)

Question: when a weaker model drives the tools, is the fix a simpler
API or just simpler skills? Cross the model ladder with four surfaces:

| | full API | reduced API (facade) |
|---|---|---|
| **full skills** | today's product | same verbs collapsed to `find(text)` / `read(handle)` / `more(handle)` with fixed defaults, mapped onto get/search in `agent.py`; no server change |
| **simplified skills** | skill cards only (summary + `answers:`), no bodies; or a one-path rewrite per task type | both |

Model ladder: a frontier Claude, a mid Claude, Haiku, and two open
models (a Qwen tier and gpt-oss-120b or a DeepSeek flash tier), routed
as `scripts/llm_eval/run_matrix.py` already does through the router
switch. Metrics as above plus cost per correct answer. Read it as a
heat map: where error rate and calls-to-answer fall when the API is
reduced but not when skills are simplified, the API is the problem;
the reverse says documentation is. The scripted caller is again the
control.

## Chunking and corpus axes (Reto, 2026-11-09, fourth ask)

Chunking is index-time, so each variant is a re-ingest of the fixture
(cheap at 30–200 papers). **Gold is therefore stored as character spans
in the source text, never as chunk ids**; recall is span overlap with a
rendered chunk. Without this every chunking arm would invalidate the
gold.

| arm | what changes |
|---|---|
| `chunk-fixed-256` / `-512` / `-1024` | fixed token windows, no sentence awareness |
| `chunk-sentence-pysbd` (today) / `chunk-sentence-yasbd` | sentence-aware splitting; the splitter itself is an arm (`utils/sentences.py` wraps pysbd; a YASBD swap was proposed on its vendor benchmark — decide it here, with span gold, not there) |
| `chunk-section` | structure-aware: QASPER's own paragraph/section boundaries |
| `chunk-overlap-N` | overlap added to the winning size |
| `chunk-parent` | retrieve small, render the parent paragraph |
| `chunk-contextual` | contextual chunk embeddings (`contextual-chunk-embeddings.md`) |
| `no-links` | machine-written stance/provenance edges removed (the `graph-gardener-value.md` question) |
| `distractor-×N` | the fixture padded with N× unrelated open-access text (Gutenberg / PMC-OA) to measure recall and tokens under corpus growth |

Corpus choice, settled: **QASPER is the task corpus** because it ships
questions with evidence a stranger did not write. Open-access pools
(Gutenberg, PMC-OA, arXiv CC-BY, S2ORC ODC-BY) carry no gold, so they
serve only as **distractor padding** and, later, as the pool for our own
cross-paper tasks — labelled, and flagged in the paper as self-authored.

## Analysis — factorial design, not one-at-a-time (Reto, 2026-11-09)

One-at-a-time ablation cannot see interactions (keywords may matter only
when summaries are off). The six memory switches are binary, so the
scripted tier runs the **full 2⁶ = 64-arm factorial** (deterministic,
minutes); the LLM tier runs a resolution-IV fractional factorial (16
arms) per model. Every run appends to one long table
`task × arm × model × seed → tokens, calls, correct, first_pick, errors,
wall, cost`; the analysis reads only that table.

- **Axes**: PCA / factor analysis on the outcome vector → expect an
  "efficiency" and a "quality" component; those become the paper's two
  composite scores.
- **Contribution per layer**: mixed model
  `outcome ~ switches + switch pairs + model + task_type + (1|task)`;
  main effects with CIs are the per-layer contribution, pairwise terms
  the interaction heat map, Shapley values over switch subsets the
  order-free headline figure.
- **Surface and model axes** enter the same model as factors (`hints`,
  `row_repr`, `title_style`, `api`, `skills`, `model`), so "simpler API
  or simpler skills" is the `model × api` vs `model × skills` term.
- **Budget**: ~100 tasks × 64 arms ≈ 6,400 scripted rows per seed; the
  LLM tier at 16 arms × 5 models × 100 tasks is 8,000 calls per seed —
  one seed first, judge pinned, CIs reported.

Slice 11: `scripts/bench report --analysis` — factorial arm generator,
mixed model, Shapley, PCA; depends on slice 5.
Slice 12: chunking, `no-links` and distractor arms (re-ingest variants,
span-based gold); depends on slices 3–5.

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

- **Fixture corpus**: a subset of **QASPER** (AI2; 1,585 CC-licensed
  full-text NLP papers, dataset CC BY 4.0, pinned HF revision) — 30
  papers for `--quick`, 200 for the full run — so `lookup` and
  `grounding` tasks come with published paragraph- and sentence-level
  gold and public evidence-selection baselines to compare against.
  Manifest records ids, sha256, licence; never prod text. Cross-paper
  retrieval check: LitSearch (MIT, 64K abstracts, gold paper ids).
  Stance via SciFact only as a research-only annex (CC BY-NC 2.0).
  Memory-benchmark control: LongMemEval (MIT). Source:
  perplexity-research:605814, verify before citing.
- **Task set + gold**: QASPER's own questions for `lookup`/`grounding`;
  our additions for `cross-paper`, `stance` and the memory kind over the
  same papers, committed JSONL in the `retrieval_qu164903.jsonl` shape
  (`{id, type, question, answer, evidence, gold_chunks, labelled_by}`);
  every added label `reto` or marked `model:<id>`.
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
8. Surface-ablation arms on the LLM-agent tier (descriptions, hints,
   index row representation, truncatable titles); depends on slice 5.
9. Observational baseline: mine prod tool-call WARNING lines for error
   rate per verb × kind and `next:` uptake; read-only, no dependency.
10. Model × surface arms: reduced-API facade and simplified-skills set
    in `agent.py`, model ladder through the existing router; depends on
    slice 5.
0. ~~Feasibility: import closure of the memory subsystem.~~ Measured
   2026-11-09 (`scripts/_memory_closure.py`, td605609): eager closure
   315/1407 modules (22 %), but it reaches shapely (`store.store` →
   `_pcb_ops`) and ase (`_measures_ops`) eagerly and rdkit/matplotlib/
   torch one lazy import away; `precis.dispatch` alone pulls 182 modules.
   Verdict: **ship the full image for the first paper**; the
   `precis_memory` package extraction is its own thread, seams named in
   the report: `precis.dispatch` handler registry, the per-domain store
   mixins (`_pcb_ops`, `_structure_ops`, `_cad_ops`, `_measures_ops`),
   and the `workers.scheduler`/`job_types` fan-out.

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

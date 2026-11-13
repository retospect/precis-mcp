---
id: precis-job-types-help
family: work
title: precis — the job_type registry (agent + internal lanes)
summary: registered job_type table for kind='job' — fix_gripe, diagnose_gripe, plan_tick, news/briefing, draft_export, conflict_sweep, quest/doctor ticks, remarkable sends, paper_ingest, embed_batch/derived_drain — plus memory processing via mode='process'
answers:
  - what job types are available / which job_type do I pick?
  - what does a given job_type do and which executor runs it?
  - how do I process a memory's embeddings before testing recall?
  - which enrichment stages can I request on a selected memory?
  - which job type runs a paper ingest / draft export / briefing?
applies-to: put (kind='job', args={'job_type': …}); get/search (kind='job')
tags: workflow, verbs
kinds: job
status: active
---

# precis-job-types-help — the job_type registry

Every `job_type` a `kind='job'` submit may name, minus the two
families with their own skills: the dark-factory / PR
lanes live in [[precis-job-factory-help]], the release lanes in
[[precis-job-release-help]], the compute and code
lanes (`struct_relax`, `pourbaix_bulk`, `surface_coverage_scan`,
`sandbox_run`, pathway/pcb/retrosynth) in
[[precis-job-observe-help]]. The submit shape — `parent_id` via
`args=`, idempotency, rejection reasons — is
[[precis-job-help]]; a `job_type` here submits the same way.

## What job types are available?
## List the registered job_types

Agent and internal job types; the compute and sandbox types are in
[[precis-job-observe-help]], the factory lanes in
[[precis-job-factory-help]].

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `fix_gripe`         | `claude_inproc` | Fix a gripe and land it. **Lane OFF since 2026-10-02**: nothing auto-mints it; a hand-submit skips at the push preflight without a credential |
| `diagnose_gripe`    | `claude_inproc` | Read-only root-cause diagnosis of a gripe — clones the repo (never branches/commits/pushes), asks claude for a structured `DIAGNOSIS:` block (root cause + evidence + a proposed-fix sketch + `Confidence: 0.NN`), and appends it as one `DIAGNOSIS (auto, job <id>):` `gripe_comment`. Never flips the gripe's `STATUS`. Params: `gripe_id`. **Off on prod** (the `diagnose_scan` minter pass is switched off by ruling, 2026-10-02); a hand-`put` diagnose job runs regardless. `PRECIS_DIAGNOSE_CLAUDE_MODEL` overrides the model (default: `Tier.BIG`, cheaper than `fix_gripe`'s FRONTIER). When auto-promote is enabled and confidence ≥ 0.8, the gripe is tagged `auto-fix`, now inert (the `fix_gripe` lane is off) — default off. |
| `plan_tick`         | `claude_inproc` | One planner-coroutine tick of a `meta.llm_tier`-set todo. Params: `model` (required), `timeout_s`, `decompose`, `select`, `envelope` — a named box (`'scout'`: WebSearch only, no MCP at all; the worker assembles its prompt and files its JSON reply); declared means required — an unknown name fails closed and a fenced tick is pinned to `claude_agent` — and `scout` (`{folder, quest}`, required with `envelope='scout'`) (see `precis-recurring-help` §Restricted tick) |
| `news_poll` / `briefing` | `claude_inproc` | News ingestion / daily briefing          |
| `money_refresh`     | `claude_inproc` | Refresh the `fx_rates` / `price_index` caches from the ECB and World Bank (deterministic; driven by a weekly recurring todo — minted by ops, never by hand) |
| `draft_export`      | `claude_inproc` | Compile a draft to PDF/DOCX                   |
| `elsevier_abstract_backfill` | `claude_inproc` | Re-arm an operator-confirmed Elsevier preview cohort for re-fetch (stamps the existing `markup_refetch`/`oa_requeued` pins; bodies, hashes and events stay until a validated replacement ingests). Params: explicit `ref_ids`, `expected_count` (must equal `len(ref_ids)`), `dry_run` (default `true`). No provider or model call; never derives the cohort from body length. |

## Campaign, deferred and external-agent job_types

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `good_search`       | `coordinator`   | Deep paper-search campaign — normally minted for you by `search(kind='paper', q=…, good=True)`, not submitted by hand (see `precis-search-help`) |
| `good_search_triage`| `claude_inproc` | A `good_search` triage batch (internal — the campaign spawns these itself) |
| `conflict_sweep`    | `claude_inproc` | One claim hub hunts its opposition: negated-paraphrase ANN over the corpus, `paper_rank`-budgeted LLM verify, `disputes` edge on a confirmed contradicts, `meta.conflict_search` coverage ledger stamped. Minted for you at claim mint and by the approve page's freshness check (dark unless the `conflict_search` service is lit); by hand: params `hub_id` (the `fi<id>` ref_id), optional `refresh` (sweep even if already current — covered passages are still skipped). See `precis-taproot-help`. |
| `deferred_llm_call` | `coordinator`   | A synchronous surface's LLM call run later, after a Claude quota or budget window (internal — the web follow-up mints it when the model is paused; it sleeps until `retry_at`, then appends the real answer to the conv) |
| `devin_task`        | `devin_api`     | Hand one task to a cloud Devin session via the official API v3 — the executor mints the session, polls, collects the result and any PR links. Params: `prompt` (required), optional `title`, `repos`, `tags`, `max_acu_limit`, `resources.wall_seconds`. Drained by the `job_devin_api` service, which ships **dark** — gated off every cycle until a human operator lights a `service_config` row; the job carries no prod-write credential, and a claimed result is evidence a consumer verifies by sha, not truth |

## More registered job_types — the rest of the internal lanes

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `doctor_tick`       | `claude_inproc` | One LLM "doctor" pass over the fleet's own published health surfaces — the self-healing spine's Layer-3 judgment: gathers, classifies by ratio, diagnoses, authors the daily report, files/annotates gripes (internal; see `precis-health-digest-help`) |
| `quest_tick`        | `coordinator`   | One slice of a quest's perpetual catalyst loop (harvest → propose → dispatch → wait) — internal, minted by `quest_loop_reconcile` (see `precis-quest-help`) |
| `reground_claim`    | `claude_inproc` | Reground a taproot claim hub — audit/prune proxy evidence, re-discover deeper primaries, add-first apply (checkpointed); `mode='verify'` diffs intent vs committed (see `precis-taproot-help`) |
| `draft_refresh`     | `claude_inproc` | Bounded, section-scoped refresh of a living draft — critique + rewrite against corpus + research-arm evidence, growth-gated apply |
| `cad_discuss`       | `claude_inproc` | Discuss a cad design (tool-less claude, threaded, read-only) — answers questions, proposes nothing (see `precis-cad-help`) |
| `diagram_propose`   | `claude_inproc` | One figure/mermaid draw-with-me turn — builds/verifies the diagram from seed chunks, reconciles node→chunk bindings (see `precis-mermaid-help`) |
| `structure_propose` | `claude_inproc` | Turn an instruction into proposed structure ops (tool-less claude, propose-only — a human applies them) |
| `reading_brief`     | `claude_inproc` | Compose the morning reading-brief cast as a dated draft (see `precis-audio-help`) |
| `remarkable_send`   | `claude_inproc` | Export a draft in reMarkable mode and upload the PDF to the tablet — siblings `remarkable_papers_send` (cited-source PDFs) and `remarkable_reading_send` (per-source reading edition) |
| `paper_ingest`      | `job_inproc`    | Acquire one DOI-requested paper — verifies the DOI (Semantic Scholar, then the DOI registries), mints the stub and pins it to the `fetch_oa` queue. Internal: minted by `get(kind='paper', id='<unheld DOI>')` (gr551840), idem key `paper_ingest:<doi>` — a get repeat reuses the live job, a terminal job inside 24h blocks the re-mint |

## Queue-drain job types — embed_batch and derived_drain

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `embed_batch`       | `job_inproc`    | Bounded embedding work; memory `mode='process'` creates a request pinned to the selected current body/header revision |
| `derived_drain`     | `job_inproc`    | Bounded work order draining a SMALL-tier derived LLM queue (summarize/classify) — cloud-only lane |

## Process one memory before testing recall

```python
# After the memory put/edit:
get(kind='memory', id=N, view='processing')
put(kind='memory', id=N, mode='process',
    args={'stages': ['embeddings'], 'action': 'promote', 'wait_s': 5})
get(kind='job', id=J)  # returned handle
get(kind='memory', id=N, view='processing')
# Test only after current body/header embeddings are ready.
search(kind='memory', q='<future recall question>', view='index')
```

Use the memory write to create the embedding request; a generic queue-drain
job does not attest that a selected memory was processed. One memory, at most
32 current body/header chunks, priority 1 without preemption; `wait_s` is
0–10 seconds. The receipt pins chunk IDs, fingerprints and embedder, reuses
active work, and reports `queued`, `running`, or artifact-backed `ready`.
Job success alone is insufficient. Edits require a request for the new revision.

`action='retry'` handles recorded failures; `action='recompute'` explicitly
invalidates completed embeddings. A repeated completed recompute reuses its
receipt for that revision. Exact calls and refusal recovery:
[[precis-memory-help]].

## Choose memory enrichment stages

Omitted `stages` requests all stages; each gets its own outcome. Names match
the processing view; `embeddings` also accepts `embed:<active embedder>`.

| Stage | Selected-memory control |
|---|---|
| `embeddings` | Supported for current body and title/hook card through an inspectable worker job. |
| `keywords` | Refused: the pending-work pass has no selected-memory queue control; it needs current body embeddings first. |
| `summary/summarize:rake-lemma` | Refused: the pending-work pass has no selected-memory queue control. |
| `summary/llm_summarize` | Refused: the drain cannot target this revision; reader salience also changes recall and is not a processing control. |
| `categorization` | Refused when applicable: dream move processing has no revision-attested job receipt. |

Ineligible stages report `not applicable`. For a refused stage, inspect the
processing view and ask the worker operator for targeted support.

Default scheduling is memory-first (gr563702): embeddings, keywords, RAKE
and LLM summaries all claim memory chunks in an interactive lane ahead of
the corpus backlog, and `axis:move` categorizes only memory refs. Every
claim batch reserves a floor of slots for bulk work, so papers keep bounded
progress — interactive work reorders, never starves. The processing view's
`lane=`/`phase=`/`queue_ahead≈` fields show where a pending stage sits; the
write paths nudge an `embed_batch` job so fresh memories embed within
seconds. Service priorities and model routes are unchanged.

## What does each job_type require to run?

Each `job_type` module declares `PARAMS_SCHEMA`,
`COMPATIBLE_EXECUTORS`, `REQUIRES`, and a `DESCRIPTION`. The
per-type recipe skills (`precis-fix-gripe-help`, …) document the
shapes for the LLM-facing call. An incompatible executor pairing
is rejected at submit, not silently coerced — the compatibility
rule and rejection list are in [[precis-job-help]].

## See also

- [[precis-job-help]] — submit shape, `args=` channel, dispatch recipes
- [[precis-job-factory-help]] — run_slice / land_slice / plan ticks / release / PR lanes
- [[precis-job-observe-help]] — compute job types + the `/…` fleet views
- [[precis-job-ops-help]] — cancel / retry / reprioritise a minted job
- [[precis-memory-help]] — the `mode='process'` write in full

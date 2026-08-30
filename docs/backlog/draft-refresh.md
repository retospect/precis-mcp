---
status: draft
title: draft_refresh residuals — external lit-search leg + refinements
---

# draft_refresh residuals

The core shipped (scan cadence `draft_refresh_scan` in
`src/precis/workers/scheduler.py`, job type
`src/precis/workers/job_types/draft_refresh.py`, section-insertion skill
`precis-draft-section-insertion`): staleness-driven per-section refresh of
`meta.draft_refresh`-opted drafts — prose-only rewrite (tables/figures/terms
preserved), growth-ratchet-gated apply, corpus-side backfill + research-arm
(serves-graph frontier) evidence, logbook + attempt-tree process memory.
Design detail lives in the `precis.workers` docstring and the job module.
Remaining open work:

- **External lit-search leg** (deferred from v1): per-section S2 query;
  genuinely new papers queued for ingest, budget ≤3 per run. Corpus-side
  backfill candidates cover the gap until then.
- **Event-triggered staleness for own-results sections**: pure age treats a
  frontier update like any other month — a frontier/finding change could mark
  the own-results section stale immediately. v1 is age-only; revisit once the
  qu202467 survey book has accumulated refreshes.
- **Nested-substructure rewrite**: v1's rewrite output is flat paragraphs; a
  section's sub-headings are preserved but their prose is not restructured
  across the boundary. Revisit if stalest-pick keeps landing on deeply
  structured sections.
- **Logbook roll-up/compaction policy** (deferred until volume hurts —
  append-only is acceptable for now).
- **Failed-job un-pin policy**: a `draft_refresh` job that latches
  `STATUS:failed` pins its section's idem_key forever (date component =
  `min(created_at)`, stable), so the scan never re-mints — operator must
  soft-delete the job row (the existence check filters `retired_at IS
  NULL`). First hit: job 202755 (stale orphan worker, unknown job_type).
  Consider excluding latched-failed jobs from the existence check or
  auto-expiring them after N days.
- **Pre-materialize citation-lens off the model host (warm-cache), and batch
  the S2 fetch.**
  - **Status (2026-08-11).** Two of the three pieces shipped; the warm-cache
    job is **deliberately deferred**.
    - ✅ **Batch the S2 fetch — SHIPPED.** `citation_lens.py::materialize_citation_edges`
      now resolves every cold paper's S2 id up front and issues one
      `POST /paper/batch` via the `fetch_citations_batch` seam
      (`ingest/citations.py::citations_batch`), per-paper write/commit/retry
      semantics unchanged.
    - ✅ **S2 no longer hammered — SHIPPED (the shared rate limiter, c487bd43,
      `docs/backlog/shared-rate-limiter.md`).** Every S2 call — including this
      inline citation-lens fetch — now coordinates cluster-wide through one
      token-bucket row, so the "angers S2" motivation below is resolved.
    - ⏸️ **Warm-cache job — DEFERRED.** With the fetch batched *and*
      rate-coordinated, the warm job's only remaining benefit is unblocking
      the serial `claude_inproc` lane while the (now one-shot, coordinated)
      fetch runs — a secondary optimization. The self-healing design needs
      `draft_refresh` **converted into a coordinator phase-machine**
      (`spawn_child` + `Yield`/`WakeWhen` park/resume + `UNPARK_CAP` degrade —
      feasible, mirrors `job_types/good_search.py`, but a real hot-path
      rewrite). Revisit *only* if the model-host lane blocking actually bites
      post-limiter; the lighter alternative if so is scan-orchestrated (scan
      mints a `draft_refresh_warm` job_inproc job at higher priority alongside
      the real job; real job reads warm cache or falls back to inline).
  - **Problem.** `job_types/draft_refresh.py::_dispatch` →
    `::_evidence_digest` → `backfill/workspace.py::assemble` →
    `backfill/citation_lens.py::materialize_citation_edges` fetches each
    newly-cited paper's S2 citation graph live (two sequential
    `SemanticScholar.get_paper` round-trips per paper —
    `ingest/citations.py::_get_references` + `::_get_citations`, tenacity
    backoff to 60s), inline in the `claude_inproc` job on the big-model
    serving host, before the `Tier.BIG` rewrite dispatch — holding the
    serial `claude_inproc` lane. Already TTL-cached (30 days,
    `citation_lens.py::_is_fresh`) and commits per-paper inside the fetch
    loop, so a bounce resumes on cold papers only — but a section citing
    many papers cold still blocks the model host on dozens of rate-limited
    round-trips. Pure network I/O + DB writes, no LLM/GPU — doesn't belong
    on the model host. (Not a diagnosed root cause of any incident: job
    203015, nano-computer:dc1506328, succeeded; its ~4 "workspace
    assembled" re-dispatches, 06:49→07:05 UTC 2026-08-11, span a busy
    deploy window against a tiny cited-set (2 findings/2 papers, ~4 S2
    calls) — deploy-bounce churn, not a slow fetch. Flagged here as a
    latent scaling fragility.)
  - **Warm-cache architecture.** Run the same evidence-assembly prefix on a
    non-model host (embed/backfill worker) to resolve+cache and discard the
    output, so the real model-host run reads warm cache.
    - Warm job calls only the caching surface — `_evidence_digest` /
      `_research_arm_digest` / `assemble`, all upstream of the `Tier.BIG`
      dispatch in `_dispatch` — never full `_dispatch` (whose tail fires
      the LLM rewrite, appends job_events, calls
      `quest/dossier.py::add_attempt`, runs
      `quest/narrative_budget.py::narrative_growth_gate`, and applies
      insert-before-retire). Same code as the real path, so a future
      lit-search leg is covered for free — no hand-enumerated prefetch
      list.
    - Cache is machine-independent: shared prod DB (`links` cites edges +
      `s2_neighbors`, migration 0106; TTL via the `citation_edges`
      ref_event) — warming host A genuinely warms host B's read.
    - **Warmth gate on the real job** — the piece that makes this robust,
      not hopeful. Prefer self-healing: the real `draft_refresh` job checks
      the cited-set's edge freshness first; if cold, triggers the backfill
      warm and re-queues itself, returning immediately, so the LLM only
      ever runs warm. Alternative: orchestrated — scan mints a
      `draft_refresh_warm` job before the real one. Recommend self-healing
      — fewer moving parts, has its own safety valve.
    - Once warm, `_evidence_digest` skips the inline
      `materialize_citation_edges` call and reads pure SQL —
      `citation_lens.py::citation_neighbor_degrees` /
      `::find_citation_candidates`.
    - Bonus: makes deploy-bounces cheap to survive — with evidence
      pre-warmed, a bounce mid-rewrite re-reads warm cache on restart; only
      the LLM call + apply re-run.
  - **Batch the S2 fetch** (orthogonal). Installed `semanticscholar` 0.12.0
    exposes `SemanticScholar.get_papers(paper_ids, fields=…,
    return_not_found=True)` → `POST /graph/v1/paper/batch`, ≤500 ids/request
    (hard `ValueError` above that), with `references.*`/`citations.*` valid
    nested batch fields — collapses up to 1000 per-paper round-trips into
    one. Caveats to design in, not skip:
    - Nested batch arrays truncate silently (10 MB total response / 9999
      citations aggregate cap, no continuation token) — detect via
      `referenceCount`/`citationCount` vs returned length, fall back to
      paginated `get_paper_references`/`get_paper_citations` (≤1000/page,
      ≤9999 total) for heavy papers.
    - `get_papers` drops not-found ids from the result list (confirmed in
      the installed client: `papers = [Paper(item) for item in data if item
      is not None]`), destroying positional alignment — match results back
      to input ids by `paperId`/`externalIds` (request `externalIds` in
      `fields`), never zip positionally.
    - The batch endpoint is 1 rps with an API key (unauthenticated shares
      5000 req/5min) — keep the tenacity wrapper; consider shrinking batch
      size for reference-heavy papers to stay under the 10 MB cap.
    - Suggested shape: `citations_batch(paper_ids) ->
      {input_id: {"references": [...], "cited_by": [...]}}` in
      `ingest/citations.py`, reusing `_to_s2_id` and the existing
      per-paper dict shaping; `materialize_citation_edges` calls it once
      instead of looping.
  - **Open question.** Why the worker restarted mid-job (203015): likely a
    deploy bounce during a busy window, not a crash (clean exit, 27 GB
    free). Not worth chasing further — long jobs will always collide with
    deploys; the fix is making the collision cheap (warm-cache + the
    existing per-paper commit/TTL checkpoint), not avoiding it.
- **Whether a quest must be STATUS:active for its book to refresh** — shipped
  answer: meta flag alone suffices (decoupled from quest tick); revisit only
  if that proves wrong in practice.

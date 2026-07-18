# precis-mcp — Open Items

Durable backlog. Only **open / blocked / deferred** work lives here; done
items are removed (history is `git log`). The mcp-critic review at
[`docs/mcp-critic-review-2026-05-02.md`](docs/mcp-critic-review-2026-05-02.md)
is the historical observation log.

> **Convention** — Status: `open`/`blocked`/`deferred` · Severity:
> `critical`/`feature`/`polish` · Owner: where the fix lives · Test: the
> regression that pins it.

---

## 🔴 High-priority

- **Consolidate `kind='cron'` and `level:recurring`** *(feature, open, high —
  owner `handlers/cron.py` + recurring spawner + `precis_web/routes/refs.py`).*
  Two unrelated "scheduled thing" concepts confuse everyone: `kind='cron'` (a
  scheduled wakeup, migration 0010) vs `level:recurring` **todos** ("Watches"
  with `meta.schedule`, which run the casts/dreamings/card-forge).
  `/refs?kinds=cron` shows empty while the real schedules live under `todo`.
  Decide: (a) fold `cron` into the recurring-todo umbrella, or (b) one
  `/schedules` view unioning both + cross-ref the help skills. Prefer (a) if
  they're the same concept. *Test:* a view lists both a `cron` ref and a
  `level:recurring` todo.

## 📜 Patent freedom-to-operate authoring loop

Shipped + deployed (main `147a984f`): sweep prior art → ingest → iterate to
patent lingo → claims against a comprehensive FTO view → `plan` scoping ledger
→ USPTO-style export with in-text prior-art citations. Design:
[`docs/design/patent-authoring-loop.md`](docs/design/patent-authoring-loop.md).

- **Validate the loop end-to-end on a real draft** *(feature, open —
  verification, not code).* Create a `doc_type=patent` draft ("+ New draft →
  Patent application"), give it an `LLM:opus` planner todo, watch a tick: sweep
  + ingest prior art (needs `PRECIS_PATENT_RAW_ROOT` + EPO OPS on the executor)
  → iterate description → write claims with the FTO `working_set` → log a
  scoping decision → export (confirm in-text cites, no `\printbibliography`).
  Watch the patent-ingest gate on the agent host + surname extraction on
  non-comma bylines.
- **Slice 7 — visual claim tree-eye + interactive `/patent/<slug>` claims
  view** *(feature, deferred).* Today the FTO digest is a text `working_set`;
  a rendered claim-family tree + interactive browser need new render/route
  surfaces. Owner: `precis_web/routes/` + a claim-tree renderer.

## 🎧 Daily audio casts — follow-ups

Daily reading-brief + nidra casts shipped + live. Owner: `reading/*`,
`workers/cast_audio.py`. Skill `precis-audio-help`.

- **Cast length calibration** *(polish, open — fix deployed, unverified).*
  2026-07-15 nidra was ~18 min vs a 45-min budget; per-segment word targets
  added in `ae37657a` but unmeasured — measure next nidra, raise the target if
  short. Morning brief came out ~4 min vs 15-min target (single-call compose,
  no floor, content-bound) — decide floor vs content-driven length. `wpm=110`
  is accurate, leave it.
- **Wire the quest lane into the morning brief** *(feature, open; td161129).*
  `briefing_cast._lane_quest` is a degrade-to-empty stub; quest slice-1 (kind +
  `serves` + `quest_log`) is live, so surface per-active-quest momentum + recent
  deeds. Nidra could bias its concept walk toward active-quest concepts.
- **Booklet (reading) lane** *(feature, blocked on reading-prep slice 2).*
  `briefing_cast._lane_reading` stub; lights up when the weekly booklet exists.
- **Cast-draft corpus hygiene** *(polish, open).* Daily cast drafts (`kind=
  'draft'`, `meta.cast`) accumulate + are embedded/searchable; add `meta.no_index`
  and/or a retention GC. Also remove leftover test drafts/episodes
  (`cast-nidra-test-546c21`, `nidra-test-546c21`).

## 🗺️ Quest layer

All slices (1 structure, 2 reweighting, 3 gaps+health, 4a–4e autonomous loop)
built + shipped + deployed. Design `docs/proposals/quest-layer.md`; skill
`precis-quest-help`; tests `tests/test_quest*.py`. Loop currently dormant (all
quests paused 2026-07-16). Remaining:

- **Link real mission quests to projects + activate the loop** *(feature, open
  — prod-data).* `put(kind='quest')` + `link(rel='serves')` deriving strivings
  from `docs/mission.md` + live research programs; re-activate quests and flip
  `PRECIS_QUEST_LOOP_ENABLED` on the melchior agent worker. Real `struct_relax`
  GPU lane on spark must be live for dispatched sims to run, not just queue.
- **Dream nomination-prompt tilt** *(feature, deferred).* Inject active-quest
  context so the dream reasons about what to nominate. Deferred: dream agent is
  gated off in prod (`PRECIS_DREAM_AGENT` unset). Owner: `workers/dream_agent.py`
  + `data/prompts/dream-prompt.md`.

**Open design questions** (resolve as steering matures): cost/credit attribution
under overlapping quests (pull = max; cost needs a split/shared-pool rule);
"promise" bid term needs a concrete proxy (frontier-improvement rate); prose
rubric → machine-measurable objective vector; the proposer (propose-next-
candidate) is the crux + least-specified; sub-quest vs achievable-goal boundary
(revisit if authors keep getting it wrong).

## 🧪 chem-tools (ADR 0056)

`route` (retrosynth) ships dark behind `PRECIS_CHEM_ENABLED`; slices 1–3 built,
slice 1 live on spark. `protein` kind (4a/4b) shipped + deployed + live (fold
proven end-to-end, pLDDT 84.7). Design `docs/design/chem-tools-integration.md`.

- **Deploy slice 2 (LinChemIn normalize)** *(feature, open).* Rebuild the
  aizynth image on spark so the shim emits `route.json` (metrics + engine-
  agnostic steps): `ansible-playbook playbooks/43-aizynth.yml`; `scripts/deploy`
  for the precis-side `parse_syngraph`/`view='metrics'`. Owner:
  `~/work/cluster/roles/aizynth`, `docker/aizynth`.
- **ASKCOS (slice 3) live-verification** *(feature, open).* Built + stub-tested,
  inert in prod. Stand up ASKCOS v2 (`PRECIS_ASKCOS_URL`) + a `roles/normalizer`
  play; **verify the Tree-Builder request/response schema against the instance's
  `/docs`** (the one unverified surface, flagged in `src/precis_chem/askcos.py`).
- **Slice 4c — ColabFold MSA engine** *(needs-decision).* De-novo single-seq is
  low accuracy (insulin A pTM 0.1). ColabFold isn't a docker image / on PATH on
  spark; clean path = containerize (`colabfold:ready`) + decide MSA source
  (MMseqs2 API vs local DBs). (The `structure` convergence half is done.)
- **Slice 5 — `sequence` kind (design) + 4c fold accuracy** *(feature, ready to
  build).* Engines chosen: **Boltz-2** (new `protein` engine, hosted MSA) +
  **LigandMPNN** (new `sequence` kind + `design` job). PyTorch-CUDA foundation
  solved: stock `pip install torch --index-url …/cu128` gives working GPU on the
  GB10 (no NGC creds). Build: a `torch-cuda` base image → Boltz-2 layer → LigandMPNN
  layer, each = a precis engine adapter + a `roles/*` mirror of `roles/alphafold`.
- **Slice 6 — chem/bio `plan_tick` executor** *(deferred).* The `precis-lab-help`
  composition skill is built; a dedicated auto-driver couples to the planner
  (the generic planner already does it).
- **MCP-surface design review — chem/bio kinds** *(design-review, filed).*
  Coherence pass over `route`/`protein`/`structure`/(future `sequence`) through
  the seven verbs: consistent `view=` naming; discovery of dark/plugin kinds;
  the **CLI/`repl` `put` arg-allowlist gap** that rejects plugin kwargs
  (`sequence`/`engine`) so only `runtime.dispatch`/MCP JSON-RPC can drive a
  plugin-kind `put`. Its own focused pass.

## 💰 Budget guardrails — global spend circuit breaker

Design [`docs/design/budget-guardrails.md`](docs/design/budget-guardrails.md).
Per-call caps + cost ledger exist; no aggregate ceiling. Devin-merge review
residuals (breaker gates every paid tier; real-cost capture from OpenRouter
`usage.cost`; enforcement-seam integration tests) are **implemented + green on
branch, unshipped** (see memory `budget_oauth_quota_split`). Remaining:

- **Piece A — cost-band affordance** *(feature, open).* Uniform `free · cheap ·
  expensive` (+ `fast · slow`) words surfaced to the model + a permissive
  "escalate freely when needful" policy line. No enforcement. Owner
  `src/precis/budget/` + `utils/llm/router.py` + `_cache_base.py`.
- **Real-cost capture** *(feature, open).* Sum the provider's actual returned
  cost, not estimates. Claude reports it; OSS/local + OpenRouter path drops
  `usage` (needs OpenRouter's `cost` field); perplexity uses a flat ClassVar.
- **Piece B — global circuit breaker (hourly + daily)** *(feature, open).* Two
  web-editable numbers (`PRECIS_BUDGET_HOURLY_USD`/`_DAILY_USD`) bounding router
  LLMs + paid fetch kinds; on trip refuse new *expensive* work (graceful
  `LlmResult.error`), auto-clear as the window ages, emit a Discord `alert`.
  Owner `src/precis/budget/breaker.py` + `router.dispatch` + cache `_fetch` +
  `/budget`.
- **Piece C — quest attribution** *(deferred).* Let `LlmRequest.source` carry a
  quest id so per-quest spend is a query over the same ledger.
- **Open decisions** (design doc): ledger union without double-count; per-model
  price-table source + upkeep; cheap-band threshold; real cap defaults.

## 🩹 asa storeless-precis incident — residual

- **conv capture silently stopped 2026-06-27** *(open, investigate — owner
  `asa-bot capture_shim` + `handlers/conv`).* No `kind='conv'` rows since then
  despite `POST /capture` → 200 and no `capture-fallback.jsonl`. Likely the same
  storeless-precis root cause; **verify after the next asa Discord turn** now
  that the double-build fix + monorepo cutover are deployed. If still broken,
  trace the shim's write path (200 despite no persisted row).

## 🔐 secrets vault (ADR 0055) — residuals

Shipped + cut over. Remaining are small/by-design:

- **Left in env by design:** `PRECIS_UNPAYWALL_EMAIL` (a mailto); litellm/openclaw
  ansible-vault secrets stay until those tools retire (sweep with litellm teardown).
- **Deferred by design (ADR 0055):** per-service DB roles + per-name ACL;
  `pg_notify` cache invalidation (currently 60s TTL); out-of-process broker.
- **Cheap/local-model research tier** *(feature, open).* precis's agent/research
  surfaces (asa, reviewers, planner, `perplexity-research` ~$0.50/call) all run
  cloud Claude with no cheap pre-filter. Add a local-model tier (ADR-0046 router
  `Tier.LOCAL_*`) for broad fan-out / low-stakes triage before paid escalation.
- **"Corpus before paid web" cost-ordering line** *(polish, open).* One line in
  `precis-research-help` + asa's SOUL: exhaust free corpus before spending on
  `perplexity-research`.

### Cluster residuals (ops, `~/work/cluster`)
- **daily_briefing references a dead `cluster` DB** — `roles/daily_briefing` runs
  `psql -d cluster` (renamed/retired); repoint at `precis_prod` or remove.
- **extract_watch uv-cache perm error on balthazar** — `~deploy/.cache/uv` has a
  root-owned `.git` blocking `uv pip install`; chown/clear it.
- **Orphan sweep from feynman/quest retirement** — installed venvs/npm bits
  (`/opt/mcps/quest`, `/opt/mcps/extract`, `@companion-ai/feynman`), quest's
  `papers` schema, unused `quest_*`/`feynman` group_vars. Harmless; sweep with
  the litellm teardown.

## 🎨 `figure` kind — deferred slices

Slice 1 shipped (interactive SVG canvas, `/figure` editor). All below are
feature extensions, ordered by value. Owner: `precis/figure/*`, `handlers/figure.py`.

- **PNG / animated-raster export** — a `figure_render` derived-lane job + a
  rasterizer (no SVG rasterizer dep today; `resvg` + declarative keyframes, no
  headless browser). PNG first.
- **three.js / `scene3d` mode** — `meta.render ∈ {svg,scene3d}`; declarative
  scene IR + trusted client renderer (never eval raw three.js).
- **Per-node chunk split** — one chunk per top-level element once per-node edits
  land.
- **Draft-embedding** — a draft includes a figure's rendered raster as an asset;
  add a `figure-in`→draft link.
- **`read(handle)` reference tool in the turn loop**; **pin full
  `precis-figure-svg` skill text into the turn prompt** (polish);
  **formalized-convention hard-checks** (opt-in palette-allowlist lint).

## 🖇️ `mermaid` kind + diagram chunk-binding (ADR 0057)

All five slices shipped; `mermaid` kind live (deployed `c7ac23db`). Design
[`docs/design/diagram-editing-and-chunk-binding.md`](docs/design/diagram-editing-and-chunk-binding.md).
Follow-ups:

- **Engine gaps — gantt / pie / sankey / C4 / block don't render** *(bug —
  owner `mermaid/mermaid.py` + `[mermaid]` extra).* The in-process QuickJS engine
  lacks browser globals (`offsetWidth`, `structuredClone`, `screen`, …). Fix:
  bump `mermaidx` when upstream ships a fuller shim, evaluate `termaid`, or
  polyfill the cheap globals. `precis-mermaid-unsupported` steers the model to
  renderable alternatives meanwhile.
- **Rich cross-kind seed rendering in `diagram_propose`** *(feature — owner
  `workers/job_types/diagram_propose.py`).* Render richer per-kind seed content
  (a figure's SVG, a cad cross-section) instead of a titled reference.
- **Self-directed drawer follow-ups** (from the shipped slice-5 upgrade, main
  `6585223d`): **mermaid L1/L2 auto-context** (add a `mermaid`-owning-draft
  reverse resolver + route `document_context_for`; figures get it free);
  **L2 semantic leg** (embed instruction entities + rank the draft's chunks, not
  just literal term hits — owner `diagram/doc_context.py`); **MCP `vocab`/`notes`/
  `element` plumbing on `edit`/`link`** *(bug)* — the exposed `edit` tool strips
  `vocab=`/`notes=`/`viewbox=` and `link` lacks `element=`, so an agent can't
  update a figure's vocab/notes or set an element→chunk binding over MCP.
- **`wip/backlog-docs` branch (primary repo)** *(polish).* One local-only commit
  `e5643873 docs(backlog)`; ship it or drop it.

## 🔵 Turn-as-job routing + context DSL *(deferred — design captured, not sliced)*

Design [`docs/proposals/turn-routing-and-context-dsl.md`](docs/proposals/turn-routing-and-context-dsl.md).
Every turn = `kind='job'`; Part 0 thread persona + cache-ordering + affinity
scheduling; Part 1 delegate-on-confidence routing; Part 2 stateful context DSL
(ADR 0036 handles + fidelity ladder). First slice = persist turn-as-job + shadow
router. Owner: `handlers/job.py` + `workers/dispatch.py` + `utils/prompt/`.

## 🟡 Unified item view (`/items`)

Slices 1–3a shipped + deployed (cross-kind search page + reading-intent flags +
`Store.search_chunks_across_kinds`). Design
[`docs/proposals/unified-item-view.md`](docs/proposals/unified-item-view.md).
Owner `precis_web/routes/items.py`, `precis_web/item_view.py`.

- **Rest of slice 3** *(open).* Promote `ItemPresenter` to the full contract
  (`preview`/`hover_preview`/`thumbnail`/`actions`, `@abstractmethod` once all
  kinds adopt); result pagination (capped at 30 today); author/source facet +
  folders + thumbnails; retire `/drive` / `/papers-needed` / `/papers/triage` /
  `/refs` / `/tags/refs` into `/items` filters.
- **Kind-taxonomy audit** *(open, coupled).* Reconcile `role`/`corpus_role` drift
  (datasheet, pres); collapse near-dup kinds (perplexity-*/websearch/web/wikipedia;
  calc/math/oracle); rewrite `precis-*-help`. No-legacy-alias license.
- **Slice 4 — "write a document from this view"** *(open).* A tailored filter is
  a serialized query → mint an authoring job scoped to exactly those refs.
- **Verification residual** — eyeball the live `/items` filter-bar JS (backend-
  tested, not visually verified).

## 🟢 Draft inline editor

Shipped + deployed, core complete (click-to-edit prose, ProseMirror + live
squiggle, split/merge, `[`-autocomplete, reveal-on-cursor chips). Design
[`docs/design/draft-inline-editor.md`](docs/design/draft-inline-editor.md).

- **Deferred extensions** *(optional, none block use):* `[`-autocomplete over
  non-paper kinds (chunks/findings); resolved-title chips; structured-block
  creation from a slash-menu; per-draft language selector for spellcheck.
- **Headless-browser verification in CI** *(testing infra, high-value).* The
  interactive editor + virtual-scroller JS has **no gate coverage**; several
  browser-only bugs reached prod. A Playwright-over-SSH-tunnel harness
  (2026-07-05) found+proved the focus bug — wire a slim version into
  `scripts/ship`: boot the web app on the test DB with a seeded draft, assert a
  clean console + a couple of core interactions. (Also listed in the arch review.)

## 🔵 Retire the `equation` chunk kind → math as `$…$`/`$$…$$` in prose

*(decided; feature/simplification).* North star: no dedicated `equation` kind —
math is LaTeX inside prose, KaTeX-rendered on read. **Drafts (278) sorted.**
**Papers (~54.6k, the bulk) — the real target, needs its own handling** (see the
deferred paper-side section below): append-only body chunks (DELETE+INSERT
re-runs the cascade at scale), produced by Marker not the LaTeX importer,
rendered by the two-pane PDF reader, and deliberately un-embedded
(`SKIP_EMBED_TYPES`). Shared work: a KaTeX-safe body normalizer (strip
`\label`/`\tag`, `align`→`aligned`, pure tested fn + gold set); numbering/`\ref`
decision; LaTeX export of `$$…$$`. **Interim** if not scheduled: just make
`equation` *render* (wrap bodies in `$$`).

## 🟢 Dark-factory build/deploy workstream

`scripts/deploy` + `/go` + `/whatneedsdoing` + post-ship follow-through shipped.
North star: `claude -w` → spec → `/go` → implemented/gated/merged/deployed. Owner
`scripts/`, `.claude/commands/`, `CLAUDE.md`. Remaining:

- **Token-lean session boot** *(partly done).* CLAUDE.md compressed; next: apply
  the same discipline to `~/work/cluster` CLAUDE.md, measure boot token delta.
- **Backlog groomer — OPEN-ITEMS half** *(open).* The gripe→`fix_gripe`-todo
  groomer shipped (`workers/backlog_groom.py`, default-OFF). The OPEN-ITEMS half
  is blocked on two prereqs: (1) `OPEN-ITEMS.md` isn't packaged into the wheel, so
  a deployed worker can't read it (needs a packaged/DB-backed backlog source);
  (2) no `build_feature` job_type for a free-text feature item. **Activation
  (ops):** flip `PRECIS_BACKLOG_GROOM_ENABLED=1` on a system worker to drain open
  gripes; watch mint count + fixer throughput before widening.
- **`/testfeature <prompt>`** *(open).* Agent loop that exercises the MCP surface
  (`scripts/exercise-mcp` seed), finds bugs, fixes, `/go`. Turn/cost-capped.
- **`/checklogs`** *(open).* Read the recent LLM-error surface (prod `agentlog` +
  `alert` + failed `kind='job'` + error `ref_events`; local logs), cluster the
  top-N recurring failures, fix root cause, `/go`.
- **Cheap-model tiering** *(open).* Route mechanical LLM work (`llm_summarize`,
  triage children, CI-fix) to a small 4B–14B model; reserve Opus for judgment.
- **Out-of-band DB-liveness monitor** *(open, ops).* The 2026-07-05 ~8h prod
  outage ran unalerted because every alerting path is DB-backed. Needs an external
  `SELECT 1` watcher on a different host (fixer host / laptop cron) → Discord on
  failure. A degradation trend-alarm (worker-log volume halving) is a cheap second
  signal.
- **Widen `scripts/ship` auto-fix surface** *(polish).* Auto-fix + amend anything
  the gate can resolve without judgment (import sort, trivial mypy stubs).
- **Deferred:** holdout scenarios (anti-overfit eval outside the repo); digital-
  twin fidelity (richer stubs); auto-deploy as a daemon (vs `/go`-chained).

## 🟠 Worker liveness + observability

Slice 1 (observability: boot-event row + `worker-restart`/`dead-worker` nursery
detectors + Discord webhook) shipped + deployed. Owner `workers/nursery.py`,
`cli/worker.py`, `alerts.py`, cluster repo.

- **Set `PRECIS_OPS_ALERT_WEBHOOK` on system-profile workers** *(ops, open).*
  Critical push is dark until set (cluster ansible env); until then
  worker-restart/dead-worker alerts only land in `/alerts`, not proactively.
- **Tier B — lease as the single job-substrate liveness authority** *(open).* Let
  the reclaim path take over a `running` job whose lease expired (requeue-from-
  checkpoint), then retire the sweeper's `PRECIS_STUCK_JOB_HOURS` clock. Needs a
  per-job attempt cap. Owner `executors/_common.py`, `sweeper.py`,
  `executors/coordinator.py`.
- **De-SPOF the agent worker** *(open, ops — highest-value).* `plan_tick` runs
  only on melchior operationally (hermes `~/.claude` OAuth + `PRECIS_MCP_CONFIG`).
  Provision a second agent host (caspar/balthazar) with the OAuth state + an
  agent daemon. No code.
- **Co-location relief** *(open, ops).* Get the ~73 G `mlock`'d llama.cpp weight
  off the agent host (or drop `--mlock`) so jetsam stops targeting the worker.
- **Sandbox substrate** *(open, big lift).* The `sandbox_run`/`claude_docker`
  substrate (ADR 0048, `docs/proposals/sandbox-run-substrate.md`) runs ticks in
  isolated containers — subsumes the SPOF + co-location. The durable north star.
- **Config-drift guard (cluster repo)** *(open).* A deploy assert that deployed
  launchd plists match rendered templates (analogue of the venv-commit assert).
  Owner `redeploy-precis.yml`.

### docx / EndNote export — validation-pending
Native EndNote CWYW export shipped (`export/endnote.py`). Round-trip correctness
can only be confirmed by opening the export in real Word+EndNote + "Update
Citations and Bibliography" — Reto is testing. Open notes: `EN.Layout` hardcoded
to `"Annotated"` (make a param if requested); docx `[dc<id>]` cross-refs render
as plain text not Word `REF` fields (pre-existing, low-pri); `[pc<id>]` cited-
passage embedding shipped but round-trip unverified (EndNote drops Research-Notes
on library import; retry with `<custom1>` if persistence wanted).

## 🟢 Chunk-tag classifier (ADR 0047) — remaining

Cascade shipped + deployed + validated. Design
`docs/design/chunk-classifier-cascade.md`. Owner `workers/classify.py`,
`data/axes/`, cluster env.

- **Enable continuous corpus tagging** — worker pass deployed default-OFF; flip
  `PRECIS_CLASSIFY_ENABLED=1` to drain the remaining ~1.29M chunks on the free
  `summarizer` model. Watch load.
- **Tier-2 escalation (optional)** — `PRECIS_CLASSIFY_ESCALATE_MODEL=claude-haiku-4-5`
  to push own-claim precision past 91% (~$200-400 on the residual). Was 429-blocked
  in dev; retry when free.
- **Ref-axis production runner (`classify-papers`)** — not built. Only `material`
  (93%) + `transport` (97%) clear the gate on the free model; `domain`/`studytype`/
  `property` need a stronger model. Walk `paper` refs, apply `applies_when` gates,
  write ref tags + `meta.processing.<axis>`.
- **Better table detection (polish)** — the free Tier-0 `numeric_ratio` heuristic
  catches only 0.1%; a pipe/tab/repeated-token heuristic would recover the free
  furniture drop.

## 🔵 `serverInfo.title` not set *(blocked upstream)*

*(polish — owner `src/precis/server.py:129`, test
`test_serverinfo_carries_title`).* MCP spec 2025-06-18 §A1 recommends a
`serverInfo.title`; `FastMCP(...)` takes no `title=` kwarg. One-line fix once
FastMCP accepts it — file the request when the next mcp-critic pass surfaces it.

## 🟠 LLM-confusion residuals (from prod plan_tick transcripts)

Root causes (tex workspace-authoring, addressing, merged-handle redirects,
embedder-warmup race, nanotrans_auto spin) all fixed + deployed; a
`plan-tick-spin` nursery detector was added. Parked (none a bounded fix):

- **Chunk-handle (`pc<id>`) of a merged paper doesn't redirect** *(design
  limitation).* `resolve_handle` follows `superseded_by` for record handles only;
  a merged paper's chunks are soft-deleted with different `chunk_id`s. A real fix
  needs a chunk-level supersede mapping at merge time — investigate before building.
- **`plan-tick-spin` detects but doesn't auto-pause** *(behavior extension).*
  Auto-pausing (an `open` tag the doable view excludes) would stop the burn but
  risks halting legitimate long-running planners — needs a progress-signal, not a
  count. Backlog.
- **Ops: cull orphaned tex refs from the nanotrans_auto spin** — dozens of
  duplicate `\section{…}` refs with `workspace=∅`. A one-off cleanup query.

## 🔵 Tool-friction reflection + dream diversification

Spec `docs/design/tool-friction-reflection-and-dreams.md`. Part A (end-of-run
tool-friction footer, `utils/friction_reflect.py`) + the Part B lens seed are
built default-OFF; lens seed rehomed to first-class oracle traditions (shipped).

- **Enable Part A in prod** *(open).* Flip `PRECIS_FRICTION_REFLECT=1` on the
  melchior agent worker *once a downstream grouping/dedup lane exists* to absorb
  `friction` gripes, else raw wishes pile up untriaged. Gauge junk-rate.
- **Gripe → agentlog link (Part A)** *(open).* Link each `friction` gripe to the
  run's 30-day `agentlog`; the filing agent doesn't know its own agentlog id at
  `put` time → needs post-hoc stitching (join by time+source) or an id threaded
  into the run context. (Stopgap: self-tags `friction-model:<model>`.)
- **Dream mode rotation (Part B)** *(open).* Rotate the cycle's *deliverable*
  (connection / library-gap / open-question / consolidation / analogy), not just
  the lens. Deferred: needs surgery on `dream-prompt.md` (connection shape is
  hardcoded into Step 6).
- **Active dreams (DFT / CAD / compute lanes)** *(deferred — wanted).* An
  `active-build` dream mode that kicks a derived-lane job (DFT relax, `cad_propose`,
  structure relax) on a surfaced subject, then connects the result back into a
  memory. Gate behind the load ceiling + a budget cap.

### Paper-dedup / hygiene residuals (ops-gated, not repo bugs)
- **Run Bucket B on prod** — `precis resolve-metadata` (dry-run) over the 94
  `needs-triage`, inspect auto/review/discard lanes, then `--apply`. Network-bound
  (Crossref/S2), on-cluster only. Expected ~20 DOI-track + ~40 title-track auto.
- **Standing worker for future id-less stubs** — build after the CLI proves the
  resolution on prod.
- **id-bearing stubs that title-match a held paper (49)** — deliberately NOT
  auto-merged; real merges need cross-id (S2) equivalence proof → review lane.

## 🔵 Platform-specific test bugs (Windows + macOS Python 3.12)

*(polish, open).* CI workaround: `continue-on-error` on the affected matrix legs
(Linux + macOS-3.11/3.13 still gate). Owner `tests/test_python_*`.

- **Windows (27 tests)** — the python-handler write path uses `os.O_DIRECTORY`
  (Unix-only) for fsync → `AttributeError`. Fix: branch on `sys.platform`, no-op
  fsync on Windows. Plus `test_parse_expands_tilde` asserts a Linux tilde path —
  assert against `os.path.expanduser("~")`.
- **Python 3.12 setprofile + urllib.parse circular import (5 runtrace tests)** —
  the tracer subprocess raises a partially-initialized `urllib.parse` import;
  3.11/3.13 + Homebrew 3.12 unaffected. Likely fix: defer the profile install
  until after `urllib.parse` is imported, or run the tracer via `-S` + explicit
  `site.main()`. Carries `@pytest.mark.xfail(strict=False)` gated on 3.12.

## 🔵 OQ-11 — verify FastMCP server-pinned-prompt support

*(polish, verification only; design ships either way).* Does MCP 2025-06-18 +
FastMCP 1.x let a server flag a `prompts/list` entry as "render at session
start", or is the tag client-side only? Read FastMCP `prompts/list` handler +
MCP §prompts. The answer decides whether we can drop the redundant banner line.
Owner `mcp_modalities.py::register_skill_prompts`; artefact
`docs/design/mcp-cold-start-token-budget.md`.

## ⏸️ Snoozed — blocked upstream

- **Dependabot #44 — `transformers` <5.3.0 RCE (high).** `Recheck-after:
  2026-07-18`. `Unblock-when:` `marker-pdf` drops its `transformers<5.0.0` cap.
  Today every `marker-pdf` (≤1.10.2) pins `transformers<5.0.0` and precis needs
  marker (`[paper]`), so `>=5.3.0` is unsatisfiable as a lockfile bump alone.
  Tolerable: exploit surface ~nil (precis only loads the trusted bge-m3 embedder,
  never a user model path or `trust_remote_code`). **Recheck:** re-run `uv lock
  --upgrade-package transformers`; if it reaches ≥5.3.0 take the fix + validate a
  sample re-embed for cosine drift; else bump `Recheck-after` +2 weeks.

## 🔵 Paper-ingest `equation` chunk kind — retire later *(deferred)*

*(feature — owner `ingest/{marker,pipeline,literature}.py`).* Companion to the
done draft-side retirement. ~54.6k `equation` chunks are `kind='paper'` (99.5%),
minted by the Marker PDF path, rendered by the two-pane PDF reader (so the
"renders as raw `<p>`" motivation doesn't apply), and deliberately un-embedded
(`SKIP_EMBED_TYPES`). Migrating requires deciding the paper-equation **embed
policy first** (strip-to-placeholder? keep skipping? a `math`-marker paragraph the
embedder skips?), then change the Marker classification + batch-migrate the 54.6k
chunks (throttle the cascade). Until then the FK row stays alive.

## 🔵 CAD — spoked-wheel spokes don't bridge rim↔hub + no job-log link

*(feature — owner `cad/` geometry + `precis_web/routes/cad.py`; reported on
`/cad/make-a-spoked-wheel-with-a-mounting-bracket-v2`).*

1. **Spokes don't connect rim to hub.** The spoke op `spoke cyl:r2.5h28 polar
   n16 r26 z` centres spokes at r=26 spanning ±14, reaching neither the rim wall
   (~34–40, `torus:R40r6`) nor the hub (r12). A model-parameterisation problem —
   worth a spoke-radial-length lint / connectivity check fed back into the propose
   loop so a disconnected result is caught before it lands.
2. **No link to the failing job from the CAD page.** The page shows "answer
   failed — see the job log" (job r50911) but renders no link. Surface a link to
   the owning job when a propose/derive step fails.

## 🔵 OA acquisition + structured ingest + external search *(roadmap; little built)*

*(feature — owner `workers/fetch_oa.py`, `ingest/`, search/discovery).* Root
diagnosis: "it's OA but we don't have it" is publisher-side Cloudflare/Akamai
`403` (Wiley, bioRxiv, science.org, MDPI) — TLS/fingerprint/IP-reputation, **not**
a UA gate, so `_BROWSER_UA` is dead for this class. Prod nodes have open egress.

**Cascade design (revised 2026-07-08):** free legs first (publisher-deterministic
→ PMC-OA JATS → arXiv → Crossref/OpenAlex `oa_url`, all $0, version-of-record),
then **OpenAlex Content API** as the first *paid* fallback (~$0.01/file, gated by
`has_content`, from the fixed host `content.openalex.org` — kills the whole
Akamai/Cloudflare-403 class publisher-agnostically, verified vs ref 53423), ahead
of a paid web-unlocker proxy (last resort, ToS-grey, off by default; **never
Sci-Hub**). Prefer GROBID **TEI** for text/chunks when present, still store the
PDF for the reader + highlight coords.

**The 9-item roadmap (interdependent):**
1. **PMC OA / Europe PMC fetch leg** *(keystone).* DOI→PMCID → OA package
   (`.tar.gz`: JATS + figures + supplementary) or `oa_pdf`. Biomedical only —
   whiffs on MDPI/chemistry (hence #1b).
1b. **OpenAlex Content leg** *(co-keystone).* §B above — publisher-agnostic paid
   fallback; **built (unshipped)** as `_try_openalex_content`, double-gated
   `PRECIS_OPENALEX_CONTENT_KEY` + `_AUTO` (default OFF).
2. **bioRxiv/medRxiv S3 leg** — for `10.1101` preprints not in PMC (requester-pays);
   add preprint→VoR dedup.
3. **Paid web-unlocker proxy** — Cloudflare-only-OA not in PMC/S3; config-gated,
   off by default; CC-licensed only.
4. **Supplementary / methods ingestion** — the PMC OA `.tar.gz` bundles SI; design
   the storage shape (child refs `has-supplement` vs extra chunks).
5. **JATS/TEI structured ingest** — `extract_blocks_jats(xml, paper_id)` emitting
   Marker's block-dict shape reuses the whole downstream + `mathnorm`. Phase 1
   (new papers, prefer-XML, keep PDF) low-risk; Phase 2 (re-ingest existing PDF
   papers) is a **hazard** — citations anchor by string `source_handle="slug~ord"`,
   so a re-chunk restales them → must reanchor by `source_quote` text + snapshot at
   ref scope + add an `ingest_source` marker column; Phase 3 = stable per-chunk
   `handle` + citation-by-quote.
6. **Parallel scholarly-graph providers** — fan out `{OpenAlex, Crossref,
   OpenCitations, Europe PMC, Lens}` + RRF-fuse (robust to cross-lingual score
   gaps), dedup by DOI→title-fuzzy. OpenAlex/Crossref clients already exist. Lens
   adds paper↔patent linkage.
7. **Chinese-lit abstract discovery** — abstract-level via OpenAlex/Crossref +
   translation; **not** CNKI full-text scrape.
8. **Historical & foreign-language archive import** — bulk, scan-derived,
   identifier-less. Bulk fetcher (IA/HathiTrust/J-STAGE) + copyright-era gating
   (pre-~1930 PD = full; in-copyright = index/abstract-only) + specialized OCR
   (Fraktur/Cyrillic/CJK). **Pilot: German *Chemische Berichte* (1868–1997)** via
   IA + HathiTrust. Legit routes only; no Sci-Hub.
9. **Measure bge-m3 cn↔en placement for technical content** *(Reto's ask —
   measure, don't assume).* Probe the live embedder (`POST /embed`, port 8181)
   with N zh technical abstracts + English equivalents; report cross-lingual vs
   same-language cosine gap + top-k retrieval. RRF-per-language-pool mitigates the
   clustering bias.

**Bulk arm (§D — "set up for a big pass"):** a shared **bulk-ingest substrate**,
unified with the historical importer (#8). Money fact: OpenAlex free S3 snapshot =
**metadata only** (index/planner layer — mines *what*+priority); free bulk full
text = **S2ORC** (S2 Datasets API, keyed, no per-file charge — *priority-one
adapter*) + **CORE**; OpenAlex Content (paid) = gap-filler for the blocked residual.
`BulkSource` adapter roster (build order): `s2orc` → `core` → `oai_repositories`
(Zenodo/PMC-OA/arXiv/UoL via OAI-PMH) → `openalex_snapshot` (index-only) →
`internet_archive`/`hathitrust`/`jstage` → `east_view`. Reuse the #5
`extract_blocks_*` seam (skips Marker) + `dedup.py` + copyright gating.

**Embedding-prioritization (§E — OPEN, deliberately unsolved per Reto).** A bulk
pass dumps millions of NULL-embedding chunks; naive FIFO starves fresh on-demand
papers for weeks. Reto's instinct: "prioritize the things we already have stuff
on" — signals to weigh: referenced by todo/draft/project/citation (warm set),
recently viewed/flagged, `PRIO`/in-a-project, creation recency, lexical/keyword
adjacency. Mechanism sketch: an embed-priority ordering in the claim query; bulk
chunks stamped low-priority `meta.ingest_source='bulk'` that trickles behind live
traffic (like `llm_summarize`). Captured so the bulk pass doesn't ship without a
queue policy.

**§G OpenAlex free-metadata enrichment (wanted, built unshipped):**
`ingest/openalex_meta.py` (`fetch_openalex_work` + `normalize` + `enrich_ref`)
writes `meta.openalex` (abstract, topics, funders, fwci, 110 `referenced_works`
W-ids, ORCID+ROR authorships), registers `openalex:W…`, fills byline when empty;
CLI `precis enrich-openalex <doi|ref_id> [--backfill --limit N]`. Deferred within
G: `referenced_works` edge materialization (rides on #6; raw W-ids captured now);
topics→`ref_tags` (waits on OPEN-namespace teardown); wiring the backfill CLI into
a scheduled pass. **Verify on first real key:** OpenAlex Content auth is `?api_key=`.

**Also built unshipped:** `precis fetch-openalex <doi|ref_id>` (manual one-shot,
bypasses the auto gate); failure-reason surfacing (`/papers-needed` renders "fetch
failed: mdpi.com 403 — retry in 24h"). **NOT built:** the TEI structured path (#5),
the bulk arm (§D), the auto-leg budget cap for when AUTO is flipped on.

**Stub↔ingest dedup residuals (ops-gated):** multi-host inbox race writes spurious
`no such file` `error.txt` when watchers race the shared NFS inbox (the winner
ingests fine; recognize the wrapped file-vanished error in `cli/watch.py` + skip
silently); **187 titleless chunked papers** — `resolve-metadata` re-resolves by
DOI (32) or S2-title-search (≥0.85 gate) — run the dry-run over the cohort → gold-
check → `--apply`, then **schedule it** into `paper_reconcile` (manual-only today);
verify the 7 existing split orphans self-heal post-deploy.

## 🔊 LaTeX → speech for voice drafts

*(feature, open — owner `precis/draft/narrate.py`).* Voice-draft narration
`speakable()` currently skips math (a spoken "equation" cue, drops inline `$…$`) —
weak for math-heavy drafts. Add a `math_speech ∈ {skip, brief, full}` mode. v1
lean = a **pure-Python heuristic** (`^`→"to the power of", `\frac`→"over", greek,
operators); accessibility-grade = MathSpeak/ClearSpeak via the Speech Rule Engine
over MathML (`latex2mathml` is in hand; MathML→speech is a `node` shell-out);
per-equation author override (pronunciation-lexicon pattern). Default stays `brief`.

## 🟠 Architecture review / compaction / footguns

*(refactor, open — owner: multiple).* Cross-cutting; intentionally not one PR.
Security excluded.

**P0** — **Schema reconcile must preserve PostgreSQL ACLs** (`scripts/reconcile`,
`store/migrate.py`): `migra` diffs don't emit `GRANT`s, so new tables end up owned
by `deploy` with no `agent_rw`/`agent_ro` grants — add an ACL diff/re-grant step.

**P1 — compaction/modularization:**
- **Compact ADRs with a "Rest in Git" archive** (`docs/decisions/`). Convention
  established (ADR-0058 + `archive/` scaffold). Remaining (each its own reviewed
  change): supersede each major chain with one condensed live ADR + move
  predecessors to `archive/`. Chains: identifier (`0002/0006/0008`→`0036`),
  derived-queue (`0007/0017`→`0044`), image/embedder (`0004/0009/0012/0019`→
  `0020/0021`), figure/asset (`0034/0035`→`0057`), keystone kinds
  (`0041/0042/0043`→`0053/0056`), argument/turn-taking (`0051`↔`0054`).
- **Split `runtime.py`** (2397 lines; `_dispatch_cross_kind` 233 lines) into
  `runtime/{dispatch,search,angle,hints,error}.py`.
- **Refactor `handlers/paper.py::search()`** (600 lines) into `BylineSearch`,
  `FusedBlockSearch`, `GoodSearchCampaign`, `PaperSearchResultRenderer`.
- **Extract `EditableFileHandler`** from draft/plaintext/python/markdown/tex
  (the 160+ line `_put_anchored` methods are duplicated + diverging).
- **Split `store/_blocks_ops.py` + `_draft_ops.py`** by concern (SQL builders /
  rankers / card writers; `_draft_ops.py` has 72 functions).
- **Split `precis_web/routes/drafts.py`** (3078 lines) into per-concern modules.

**P2 — quality/discoverability:**
- **Centralize `PRECIS_` env vars** (`config.py`, `kind_gate.py`). 381 unique
  `PRECIS_` strings, `PrecisConfig` declares 19; replace ad-hoc `os.environ.get`
  with `requires_env`/`requires_secret`, then flip `PrecisConfig.extra` to `forbid`.
- **Tighten broad `except Exception`** (317 across 141 files; many hide spin loops).
- **Add headless-browser tests for the draft editor** (also above).

**P3 — type/platform/debt:**
- **Burn down the five disabled mypy categories** (`pyproject.toml`; ~184 across
  `union-attr`/`index`/`assignment`/`type-var`/`operator`).
- **Fix Windows `O_DIRECTORY` + Python 3.12 urllib circular import** (also above).
- **Recheck `transformers>=5.3.0` / `marker-pdf` pin** (Dependabot #44, snoozed).
- **Re-evaluate `ruff` ignores `RUF012` + `B905`** (can hide real bugs).

## 🛠️ Repo-dev Claude tooling — backlog

Tooling for developing precis-mcp (not the product). Bulk shipped (prose
convention, `docs/codebase.md`, `scripts/test --impacted`, `scripts/prod-psql`,
code search/index, `rtk`, navigator agent, guard hooks). Cross-session facts:
memory `repo_dev_claude_tooling.md`. Remaining:

- **Even-application follow-ups** *(refactor, open).* (1) **`state-map.md` stale**
  — factory Phase-1/2 commits shipped after its last edit; re-verify + add a
  `_Verified` stamp (it has none). (2) **136 product skills unaudited** for
  currency. (3) user-facing/runbooks/reference assumed-current, unverified. (4)
  **ADR status labels inconsistent** (case drift; several "proposed" ADRs are
  shipped). (5) **`email` worktree `0074`→`0075` renumber** before it ships.
- **More repo-dev hooks** *(feature, open — brainstormed 2026-07-18).* Turn
  CLAUDE.md prose-warnings into firing hooks. **Tier-1 (build first):** (1) PROD-
  write guard — PreToolUse on `mcp__precis__put|edit|delete|tag`, confirm when the
  resolved DSN is `precis_prod`; (2) sealed-migration guard — deny editing a
  committed `migrations/NNNN_*.sql` (ADR 0005; allow new + `baseline/`); (3)
  bare-`git stash` guard. **Tier-2 (nudges):** extend `map-staleness-reminder`
  (new migration→`migration-check`, new ADR→README index, new skill→overview);
  bare-`pytest`→`scripts/test` nudge. **Tier-3:** PreCompact hook = `memory-lint`
  + "persist residuals to OPEN-ITEMS first"; Stop-with-dirty-worktree reminder
  (marginal).
- **Mutation testing via `cosmic-ray`** *(polish, blocked-on-adoption — owner
  `pyproject.toml` + nightly).* `mutmut` is incompatible with our `-n auto`;
  `cosmic-ray` runs the test command as a subprocess so `pytest -n0` works. Scope
  to one pure-logic module (SSRF guard), nightly.
- **`subsystem-analyst` (opus) agent** *(feature, conditional — owner
  `.claude/agents/`).* A deep "how does the whole X work" synthesis subagent —
  build ONLY if the haiku `navigator` proves too shallow. Don't pre-build.

---

_Last compacted 2026-07-18: removed all done/shipped entries (history in
`git log`), condensed open items. Prior detail is recoverable from git._

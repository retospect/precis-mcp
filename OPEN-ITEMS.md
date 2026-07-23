# precis-mcp — Open Items

Durable backlog. Only **open / blocked / deferred** work lives here; done
items are removed (history is `git log`). The mcp-critic review at
[`docs/mcp-critic-review-2026-05-02.md`](docs/mcp-critic-review-2026-05-02.md)
is the historical observation log.

> **Convention** — Status: `open`/`blocked`/`deferred` · Severity:
> `critical`/`feature`/`polish` · Owner: where the fix lives · Test: the
> regression that pins it.

---
## Plan for the next big session set
- (also survey the usual thing from /whatnext)
- Do token efficiency stuff (like claude.md rules vs rationale, ensure the search tools and so on all work, an audit of (coding) prompts and a review of the last 2-3 days and what lools claude gets into that are wasteful. Lets schedule the efficiency stuff after a few hours afte token reset on THursday noon. 
- Independent local research. I want the smartest local model we can fit on the big mac to do research with ml-potential on our catalyst (and run the other research processes. Occasional opus consultations are fine and encouraged, but the bulk operational stuff should be local. Right now I don't think it does anything, the local models need some more "encouragement, do things and use tools" system prompts. The nightly and mornign meditationsl also made by this local model, lets put the biggest we can fit. 
- I want the local backup to work - if it comes to this laptop from the file server, that'll be picked up by backblaze. 
= In addition to the NO-Ammonia quest, there will be other quests. I believe the natural state is a state of ... followup (maybe that's not right) but many pending jobs for any quest to be followed up when resources allow, in time. So job priorities and sequencing are kind of important, we should discuss and plan. 
- An additional wrinkle: Local classifiers. We have many papers, we want them classified in many ways. We started a classifier system, let's review it. Should it be hierarchical.  Also, with that classifier system, we can (mcp capability follows: a draft on a topic can "audit the gap" of what classifier finds should be relevant (it's classified atomic transistor relveant) and wether it is in document/subsection where it's relevant. So it is possible to go and add new citations that came in that have been classified relevant continuously and without events - paper identified, ingested, classified for a, b, c and the a, b, c things will see it because of this cite gap analysis. )
- MS Teams posting account - for new paper summaries. As we ingest them, write a pithy 1 liner and post it to MS Teams. (this requires vandichel cooperation.) We filter by relevance to the team with a classifier too, MOF and Catalysis for macatamo uni limerick.
- audio cast: we have added a few rules to the audio cast; things like "Write Mof not MOF, write thousandfivehundred instad of 1,500 etc such that the text to speech has an easier time. Discuss if this is an appropriate course of action or if we write the report normally then "pipe it" through a filter (code or LLM?) to the syntheziser. Is there a chemistry helper (Chemistry to international phonetic alphabeth or something)
- For the no to nh3 converter, we want to see the pareto front in the document, and also, the specific energy diagrams and the atom slabs, and the attached bits at optimum for the ... most relevant cases. 
- I would expect the natural state of the system is to have many pending todos (followups from earlier tasks that got filed). We should make sure that is so (should we?) and if so, triage that. Also, there are some long running types of ... conventions for a document that should be reapplied if new source material gets added (papers/patents)
- We generally search papers well, but patents are neglected. Let's see if this can be fixed at a systemic level. 
- I'd like to have a weekly "new papers" update for: solid catalysis; MOF stuff, atomic transistors, etc.. How do we manage that? We have the paper ingestion date, that may be adequate. Should it just be tacked on to the front of the respective report, then removed when the next one comes out (so, when a new paper gets classified, we go and itegrate it in the body and add that little "weely summary" update in front also); or should we keep a running log of changes? Or should we just update the doc, and programmatically do an eye-focus-like update with only the paras touched in the last week (or an arbitrary selected time span) and their neighbours (like we could make a pdf view (or any view really, do we have a "general view" that includes these eyeballey things that can be seen by robots, pdfs, docs, and the web draft interface)).  << I like the hierarchical view well.
- I'd like to have the patent package writing (draft feature) mostly run locally, and the patent search working (i think the search is ok now). I also want to prep/check the panel screw holder device this week, you will prompt me for that. And I want to find/add the documents for filing that are supplemental to the patent so it's getting more pushbutton. I think eu/us/cn is generally good; can it be done for reasonable cost. 
- I'd like to have a few agents that come on once in a while - an ops guy that makes sure no errors are showing up and all services, apis are not causing troubles and propose solutions. That's moslty pulling together the right context automatically, and have the llm judge reasonability/status. Include db load, fs space on all machines, memory load, temperatures, weird log file entries and all that. We should auto-gather that (maybe a precis-mcp kind - the "status" view="all relevant") that sort of thing. Are queus working, are we ingesting, are we categorizign. Also a prioritization thing (are we working on the right things?) What other agents ought we to have?
- I want to make everything run through the precis-mcp llm router. And wean things off opus if we can, and shift it to local models or cheaper models in the cloud. Even for coding tasks, and also for writing tasks. We can still use claude as the top dog reviewer, but we want to push all the... stupid work down, and out to other models. (Haiku is fine, deepinfra and openrouter and EU variants are good; local is best.)
- In the flashcards, precis cloze ankin 164388 and 164387: In a general way, if we make flashcards and one defines ESB and the other also, we just need just one, ie "An {{c4::ESB::abbreviation}} ({{c1::Environmental Sustainability Body::organization}}) is a framework dedicated to {{c2::ecological preservation::goal}} and {{c3::sustainable practices::methods}}". (721137 721138 are similarly --- kinda copies of eachother). 164400 is kind of weird, that is true, but what _is_ that photocrhomism, and what _are_ the other thing encompassed? Card 146392 is not a good card. C1 and C2 both are part of a very long list; it is impossible to know which one it is. Also the structure should be {{world heritage sites}} include {{site1}} {{site2}} (terse rule). 164396 is common vocab, don't need it, why did it get added? 164391 is ... not relly needed, we know. Why it is added, or better, how to adjust for more complex vocab. Also, we have precis::xxxx id numbers, lets fold those tags under precis::id::xxxx so they can be collapsed in the gui. 

---

## 🤖 asa-slack — deploy + first-light (ADR 0062)

- **Code SHIPPED (`src/asa_slack/`), NOT deployed.** Needs, in order: (1) the
  manual Slack app + Socket Mode setup and vault-token seed
  (`deploy/roles/asa_slack/README.md`), (2) run `31-asa-bot.yml` on the
  gateway first if not already (asa-slack reuses its `mcp.json`/`SOUL.md`),
  (3) `ansible-playbook 48-asa-slack.yml`, (4) a live smoke test in a real
  Slack channel — confirm threading (never posts to channel root), the
  identity log line on boot, a paper-search question actually works, a
  "kick off a job" request is refused (`Unsupported`, not just declined in
  prose), and a repeat message from the same person shows the per-person
  `memory` note working. Only unit-tested so far (kind-allowlist, conv_slug,
  identity, token-loading — no live Slack API exercised).
  Status: `open` · Severity: `feature` · Owner: `src/asa_slack/`,
  `deploy/roles/asa_slack/` · Test: manual smoke test above (no automated
  end-to-end harness for a live Slack workspace).

---

## 🧹 `scripts/coderef` — factor the exact/unique-suffix matcher

- **`resolve()`, `_dep_node()`, and the inline block in `cmd_callers` each
  reimplement the same "exact qual match, else unique `.`-suffix match against
  the symbol index" logic.** Not a bug (all three consistent today), but a
  shared `_lookup_qual(idx, qual)` helper would remove the drift risk. Deferred
  from the coderef ship on purpose: factoring it touches `resolve()`, which the
  review confirmed byte-for-byte unchanged — not worth risking the shipped
  verbs for a cosmetic dedup. Do it as its own cycle with the existing tests
  green as the guard.
  Status: `open` · Severity: `polish` · Owner: `scripts/coderef` ·
  Test: existing `tests/test_coderef*.py` (behavior must not change).

---

## P1 Update routing layer - make sure nothing does not go thru routing layer

- ticks go to claude -p right now, they should go thru routing layer so we can switch

---

## P2 Support EU llm systems

- Support edenai.co, ShareAI – Built in Romania, explicitly marketed as a European alternative to US Big Tech infra, with multi-provider routing and EU data residency emphasis.

Eden AI – Markets itself as a broad AI aggregation platform, and is frequently cited as a “European alternative to OpenRouter” with multi-vendor support beyond just LLMs (image, video, etc.).

Orq.ai – European platform oriented toward governance, observability, and team workflows on top of multi-provider AI routing.

Requesty – EU-based routing layer; CEO statements emphasize that all data processing and routing remain within EU servers (e.g., Frankfurt), with full GDPR compliance and no data leaving the EU.

EUrouter – Hosted router with EU data residency, routing to 100+ models while keeping processing inside EU data centers.

Cortecs AI – European inference gateway with smart routing across EU providers, pitched specifically for sovereign EU-hosted LLM workloads and privacy-sensitive use cases.

Tensorix, IONOS AI Model Hub, evroc – EU-sovereign inference APIs focused on open-source models and EU data centers; they are more “single-API providers” than multi-provider routers, but fill the “EU-hosted inference” niche.
^ Review and pick best 3?

===

## 🩹 Containerized-review robustness residuals

The spark *DSN-not-reaching-the-container* retry-storm is **resolved** —
`get_adopted_dsn()` re-inject into `proc_env` (`claude_agent.py:362`), proven
2026-07-19 (a real `precis-agent` container ran ~37s where it previously
`exit 1`'d on the empty DSN); regression test
`tests/test_claude_agent.py::test_container_reinjects_scrubbed_dsn`; full
root-cause is in `git log`. These robustness gaps the incident surfaced remain
open:

- **`PRECIS_MCP_DB_ROLE=agent_rw` in the review container** — reviews are
  *mostly* read-only, so the write role looks wrong; the reason it was
  `agent_rw` is the shared reviewer footer (`review.py::_footer_block`)'s
  deliberate `put(kind='gripe', …)` carve-out so a reviewer can report
  tool-friction mid-review, which a straight `agent_ro` flip would silently
  break (writes refused by the DB, `envelope.py::db_role`). **The DB-layer
  half of option (b) is now shipped**, in-repo, without a new cluster role:
  migration `0079_agent_ro_gripe_carveout.sql` adds a `SECURITY DEFINER`
  function (`public.file_gripe_readonly`) that inserts exactly one gripe
  (ref + body chunk + `STATUS:open`) and works from *any* connecting role —
  `GripeHandler._create` (`handlers/gripe.py`) now routes through it
  unconditionally, so filing a gripe already survives an `agent_ro`
  connection today; no `agent_review` cluster role needed after all. Still
  open: (1) actually flip `PRECIS_MCP_DB_ROLE=agent_ro` on the review
  container — an ops/cluster-side decision, not blocked on code anymore;
  (2) the **tool-layer** deny (`envelope.py::disallowed_tools`) still drops
  the whole `mcp__precis__put` verb for a `write:none` envelope, so a
  *generic* read-only todo/job still can't reach this function even though
  the DB would now allow it — exposing gripe-filing as its own,
  distinctly-named MCP tool (so it's simply never in `_PRECIS_WRITE_VERBS`)
  would close that gap, but adding an eighth top-level tool conflicts with
  the fixed "seven verbs" invariant asserted in `server.py` — a design call
  for Opus/Reto, not mechanical. Decide deliberately — don't blind-flip.
- **OAuth token appears in `docker inspect` `Config.Env`** — the "secret by key,
  never in inspect" goal isn't actually met (docker records inherited `--env`
  values). If that guarantee matters, move secrets to `--env-file`.

---

## 🕯️ Dark-switch audit — orphaned vs staged feature flags

**Audit done** (2026-07-22) — classification table + rationale now lives in
[`docs/conventions/dark-switches.md`](docs/conventions/dark-switches.md).
Recommend-only per the original ask: every flag on the starter list turned
out to be **intentional-staged** (a documented Phase-2 activation step
elsewhere in this file or in the code's own docstring) except one, so
nothing was deleted. Also confirmed in the same pass: `budget/breaker.py`'s
circuit breaker is fully wired on `main`, not a stray dark hook (see the
"Budget guardrails" section below).

- **Revisit `PRECIS_LAYER2_FIXER` (tex_llm_fix)** *(still open — the one
  genuine orphan/superseded candidate)*. `src/precis/utils/tex_llm_fix.py`
  (~220 lines, self-contained) is the Layer-2 chktex LLM-fixer on the
  `kind='tex'` put path, gated behind `PRECIS_LAYER2_FIXER=1` (**default
  off**), one caller (`handlers/plaintext.py:~650`). Drafts are the
  authoring source of truth now, so this dark hook is likely superseded —
  but it's low-complexity and harmless, so **leave it running dark** and
  decide keep-vs-delete deliberately later (not a mechanical rip: removing
  it also drops the Layer-2 fix-*hint* on tex puts).

---

## 🧵 Track 1 — precis-agent image (built + proven, window-wiring remains)

The §13 container-agent executor's image. **Built, distributed, and smoke-proven
end-to-end on melchior** (2026-07-18) — the concrete container-executor proof:

- **Base fixed to `serve`, not `runtime`** (Dockerfile `agent` stage). The agent
  reaches precis over MCP against the real DB + the *remote* embedder and never
  ingests/embeds locally, so it needs neither marker/torch nor the ~3.8 GB baked
  model cache — `serve` is exactly "the wheel the worker installs" (torch-free
  `builder-lite`, ADR 0021). Image **1.48 GB**, not ~5 GB; build is model-bake-free
  (~2 min) so the DockerHub-egress-blocked cluster is a non-issue (build on a
  DockerHub-reachable arm64 Mac → `docker save | ssh | docker load`).
- **Pre-existing latent bug fixed:** the `agent` stage piped `curl | bash` for
  nodesource but `system-base` ships no `curl` and the RUN never `apt-get update`d
  first → the stage *never built* (`curl: not found` → `Unable to locate package
  nodejs`). Now installs `curl ca-certificates` first, like `dev-system`/`code-task`.
- **Smoke (melchior colima, deploy):** auth-only `claude -p` → `PONG`; full path →
  `claude -p` + precis MCP (`--mcp-config /etc/precis/agent-mcp.json`) +
  `PRECIS_MCP_DB_ROLE=agent_ro` ran a real `search(kind='paper','catalyst')` → `42`.
  Vaulted `CLAUDE_CODE_OAUTH_TOKEN` (108 ch) resolves via `precis secret get`;
  the colima VM **does** route the tailscale `100.x:6432` DB (no routing gap).

**SUPERSEDES the "distribution/flip still pending" framing below — cluster
has moved well past it (2026-07-18/19, `~/work/cluster` slices 1/2A/B1/B3,
`PRECIS_DEPLOY_FROM_TREE` now the `scripts/deploy` default main `d41dab63`):**
the decentralized scheduler (migration 0074 leases) is **live** fleet-wide,
thin cron-tick/watch-poll timers retired; pure-cloud review passes
(structural/deep_review/diagram — zero local-model dep) are **relocated to
spark and live there** (deploy-owned docker, no melchior socket fight), which
is also *why* the spark DSN retry-storm above got fixed and proven; melchior's
agent-worker now runs as `deploy` not `hermes` (B1) with colima autostart
(B3). The old "distribution is melchior-only" / "flip is the window action"
bullets are stale — superseded by:

- **`PRECIS_AGENT_CONTAINER=1` is flag-ON for melchior but UNPROVEN
  end-to-end** *(feature, open).* `host_vars/melchior.yml
  precis_agent_container_enabled: true` is set (cluster repo, uncommitted) —
  containerizes melchior's remaining `call_claude_agent` passes (diagram +
  router-agentic; reviews already shed to spark). No agentic pass has actually
  claimed through the melchior container yet. Needs a live-fire verification,
  then `scripts/deploy` to make it prod-safe and commit the overlay files.
- **Capability probe + infra-fallback breaker shipped, not deployed**
  *(feature, open — owner `workers/executors/agent_container.py`, main
  `e9c915ba`).* `container_capability_ok()` (auth+bin-info+image-inspect,
  ~60s cache, fail-safe→in-proc) + a ~10-min `trip_container_unhealthy()`
  latch that catches OOM 137/image-missing/daemon-unreachable and retries the
  same call in-proc once — this is the safety net that should go out
  *before* trusting the melchior B2 flip above. Two follow-ons noted in the
  design: an empty-result assertion (cost0∧turns0∧0-toolcalls∧no-text ⇒
  raise+alert) and a `/factory` degraded-render of `capability_ok` (deferred,
  no clean seam yet).

---

## 🧵 Track 2 — litellm-retire transport-collapse

Fold the direct-`LlmClient` consumers that bypassed `router.dispatch` through it
so litellm loses its precis consumers. **LOCAL and CLOUD passes both done**
(local: main `7f24cbf0`): every former direct-`LlmClient` call site now routes
through `router.DispatchClient`. Local (`llm_summarize` / `classify` /
`paper_glossary`, `Tier.LOCAL_SMALL`) — `LlmRequest.max_tokens` (glossary keeps
2000) + `log_call=False` (per-chunk backfills add no route-log row); byte-identical
until `served_by` is seeded, then the call reroutes to the host llama-swap
endpoint instead of the litellm proxy. Cloud (`reading/cards`, `workers/briefing`,
`reading/meditation`, `reading/briefing_cast`, `Tier.CLOUD_SUPER`,
`tools_needed=True`) — folds onto `claude_agent` (a `claude -p` subprocess, direct
Anthropic OAuth) instead of the litellm proxy's `claude-opus` alias; litellm now
has no precis consumers left at all. `log_call=True` on all four (low-volume daily
casts, not per-chunk backfills) — `llm_call_log` captures real data on these
passes now. Remaining:

- **`served_by` seeding.** A prod ops step, not a code task: seed `served_by` on
  prod `llm` cards (endpoint llama-swap `:11445`, real model) → local passes
  reroute off the proxy. The flip that retires litellm's local role; cloud already
  bypasses litellm regardless (it never reads `served_by`).

---

## 🧵 Track 3 — factory Phase-2 cutover: remaining ops

Design [`docs/design/factory-console-and-scheduling.md`](docs/design/factory-console-and-scheduling.md)
(11 slices). All buildable-dark code shipped; what's left is cluster-ops —
state lives partly in `~/work/cluster` (a separate repo), verify against the
overlay before acting.

- **Tier-2 DB role-enforce (`PRECIS_MCP_DB_ROLE_ENFORCE`) — HELD** *(feature,
  blocked — owner `store/pool.py::_apply_db_role`).* Session-level `SET ROLE`
  is only correct on a direct-to-Postgres DSN, not pgbouncer's transaction
  pool (which the agent DSN uses via `:6432`) — a real fix needs a direct-pg
  route around pgbouncer, a security-posture decision, not a mechanical flip.
  `GRANT agent_ro TO agent_rw` prereq is already applied to prod.
- **Containerize the `plan_tick` + `fix_gripe` spawn seams** *(feature,
  open).* These two build their own `claude -p` argv with env back-doors
  (`PRECIS_CURRENT_TODO`/`WORKSPACE`/`AGENTLOG`, `--append-system-prompt`,
  `--bare` + `_restricted_env`) — separate from the `call_claude_agent`
  chokepoint that `PRECIS_AGENT_CONTAINER` already covers (dream/review/
  structural/deep_review/diagram). Needs its own live-container proof before
  it can containerize.
- **asa slice-0 ops** *(ops, open).* `asa_bot`'s own OAuth/run-as cutover
  (vault fallback already shipped, mirrors precis's `utils/claude_oauth`) —
  live cutover is an ordered ops sequence (seed vault → verify → flip run-as
  → scope vault read → retire hermes), not yet applied.
- **Cluster-repo overlay commit + demotion cleanup** *(ops, open — owner
  Reto, `~/work/cluster`).* `PRECIS_DEPLOY_FROM_TREE` is now the
  `scripts/deploy` default (in-repo `deploy/` tree is authoritative,
  proven via a full green fleet redeploy) but several files created during
  the cutover are still uncommitted in the overlay repo (`roles/
  precis_worker_agent/*`, `playbooks/retire-thin-timers.yml`, the
  `postgres_host`/`gateway_host`/`nas_*` inventory aliases) — commit them
  before demoting/deleting `~/work/cluster`'s now-dead role/playbook copies.
- **Plist / `service_unit` collapse** *(feature, open — deploy-day op).* The
  final "~15 daemons → 3 managed units + embedder-subprocess" consolidation;
  the abstract `service_unit` role (renders launchd|systemd from one spec) is
  built (`roles/service_unit/examples/collapsed-worker.yml`) but not applied
  anywhere. Depends on the ops items above settling first.
- **Deploy factory-console tooltips + per-host errors** *(polish, open).*
  Shipped main `ac7712fa`, needs a `precis-web` redeploy to actually render
  on `/factory`.

---

## 🤖 LLM catalog (`kind='llm'`) — wire the policy to call-sites

All 5 catalog slices (facts/reconcile, `admit()`, ledger+reviews+tote,
`select_offering` policy, task→requirement judge) shipped + deployed, dark
by construction (empty catalog ⇒ byte-identical to today). The general
golden-task eval harness (`src/precis/llm_eval/`, `precis llm eval` CLI, 5
scored axes) and the structure round-trip eval also shipped. Nothing
consumes the policy yet:

- **Wire `choose_model`/`select_offering` into deliberative call-sites**
  *(feature, open).* `utils/llm/requirement.py::choose_model` and
  `utils/llm/policy.py::select_offering` exist and are green, but no
  production call-site invokes them — every dispatch still resolves a model
  via the fixed `Tier` table. `Selection.endpoint` (the variant-precise
  OpenRouter booking) is similarly plumbed but unthreaded.
- **`/factory` model/backend console is wired to the wrong keys, and has no
  auth** *(bug + feature, open — owner `precis_web/routes/factory.py`).*
  `set_model`/`set_prio` write `service_config.model_pref`, not the
  `app_settings['llm.backend'/'llm.model.<tier>']` keys
  `utils/llm/live_config.py` actually reads for the live fleet-wide switch —
  today the switch is DB-driven only via a raw `app_settings` INSERT, not a
  browser control. No route reads/writes a backend toggle at all. Also: no
  auth on any `/factory` POST (`src/precis_web/app.py` has no auth
  middleware) — flag before wiring a control that can flip prod's LLM
  backend.

## 🔴 High-priority

- **Run the `kind='cron'` → `level:recurring` backfill against prod**
  *(ops, open, high — owner `scripts/migrate_cron_to_recurring.py`).* ADR
  0061 retired `kind='cron'` in code; the data-migration half
  (`scripts/migrate_cron_to_recurring.py`, `--commit`-gated, dry-run by
  default) has **not been run against prod** — it needs a human to review
  the dry-run report first (the old free-form recurrence vocabulary
  doesn't map 1:1 onto the new cron grammar for every shape; `weekly`
  defaults to Monday post-migration and a few `every <N> <unit>` shapes
  outside the new grammar's range are left as `cron` refs for manual
  handling). Run `uv run python scripts/migrate_cron_to_recurring.py`
  (dry-run) against prod, review, then re-run with `--commit`.
- **Retire the standalone `precis cron tick` launchd timer** *(ops, open,
  medium — owner cluster ansible, outside this repo).* The timer still
  works post-ADR-0061 (the CLI subcommand now delegates to
  `run_schedule_pass`), so this is cleanup, not urgent: flip
  `PRECIS_SCHEDULER_ENABLED` (the decentralized `scheduler` worker pass,
  §15i) on across the fleet and remove the `precis-cron-tick` plist once
  confirmed.

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
- **Verify the morning-brief depth rewrite live** *(polish, open —
  verification, not code).* The depth-first prompt rewrite (papers get
  context+claim+method+why grounded in the abstract, not a title-only
  mention; active-only quest report + decaying dormant nudge;
  `_MORNING_CONTRACT` in `reading/briefing_cast.py`) shipped with the 20-min
  target bump; confirm it's landed on a deployed brief, not just shipped.

## 📚 Topic dossiers (ADR 0060) — standing paper classification + living syntheses

Classifier slice **SHIPPED** (`src/precis/data/topics/*.yaml` +
`workers/classify_topics.py`, default-OFF `PRECIS_CLASSIFY_TOPICS_ENABLED`,
`tests/test_classify_topics.py`) — paper title+abstract → multi-label
`topic:` tags, no migration needed (marker-tag idempotency, mirrors
`paper_glossary`, not a claims table). `docs/decisions/0060-topic-dossiers.md`
+ `docs/design/topic-dossiers.md`. Remaining, design-of-record only:

- **Synthesis tick body for topic-quests** *(feature, open)*. New tick body
  in `workers/job_types/quest_tick.py` alongside catalyst-discovery's
  propose-experiment body: harvest unintegrated papers (`topic:X` minus
  `integrated-into` link) → merge into dossier `draft` → log → link.
  Decide whether `noxrr` adopts it or stays purely active-search-driven.
- **Weekly digest cast + daily-brief lane** *(feature, open)*. New cast type
  reusing `briefing_cast.py`'s pattern (shareable, fires only on activity) +
  a quiet daily lane for Reto's own visibility.

## 🗺️ Quest layer

All slices (1 structure, 2 reweighting, 3 gaps+health, 4a–4e autonomous loop)
built + shipped + deployed. Skill `precis-quest-help`; tests
`tests/test_quest*.py`. Loop currently dormant (all
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

### Quest-optimization workstream (live quest 164903 — Pd catalyst NO→NH₃)

Surfaced 2026-07-20 optimizing the first real running quest (**quest 164903**,
coordinator loop **job 166379**, dossier draft `quest-164903-dossier`). Ordered
by value.

- **`precis quest status <id>` ops CLI** *(feature, SHIPPED).* Consolidates the
  five by-hand queries into one command: logbook tail, candidate structures +
  measures + `ruled-out:*` tags, sim-job status roll (`struct_relax`/
  `catpath_explore` by `parent_id`, STATUS + created_at), coordinator-loop
  `quest_tick` job_event trail, and per-quest LLM spend/errors (`llm_call_log
  WHERE ref_id=<q>`). Read-only. Owner: `precis/quest/status.py` + `cli/quest.py`.
- **catpath lease `wall_seconds` wiring — confirmed correct, churn cause still
  open** *(investigation, done; underlying churn unexplained)*. Traced
  `PRECIS_CATPATH_WALL_SECONDS` end-to-end: it reaches the dispatched job's
  `params.resources.wall_seconds`, which is exactly the field `ssh_node.
  _lease_seconds` reads — no wiring bug (regression test:
  `TestDispatchCatpath.test_wall_seconds_env_reaches_the_job_and_the_ssh_node_lease`
  in `tests/test_quest_compute.py`). The observed ~2.5h re-lease churn (164913:
  165035/165286→165386; Pt/Cu/Ni: 165611/165614/165617→165824/6/8) is therefore
  NOT explained by this value being dropped — needs live cluster-log evidence
  (contention? a slower-than-expected full-network run genuinely outliving even
  a correctly-applied 2.5h lease?) before raising the default; don't guess a
  new number without that evidence.
- **Relax the slab box along with the atoms** *(feature, in-repo landed;
  container + bulk-relax follow-ups open — owner `structure/relax.py::_relax_ml`
  + `slab` op + the `precis-dft` container).* **Done (in-repo):** a `relax` op
  `cell` param (`"inplane"`/`"full"`) wraps the atoms in a masked ASE
  `FrechetCellFilter` (in-plane frees a/b + γ, pins the c-axis so the vacuum
  can't collapse), writes the relaxed lattice back onto the Scene, and folds
  into the run-cube cache key; plumbed through `StructureHandler.edit` →
  `_NeedsDispatch` → `struct_relax` job params → the container `params.json`;
  the quest compute lane (`run_compute_step`) asks for `cell="inplane"` on
  reaction (slab) candidates. **Remaining:** (1) the `precis-dft` container
  (`gpaw-relax`, external repo) must actually honour `params.json["cell"]` — the
  param rides the contract but the container-side variable-cell path is unbuilt;
  (2) *better for slabs* — relax the **bulk** once per (element, MLIP) with a
  full cell filter, cache the lattice constant, and have the `slab` op cut the
  surface at that MLIP-consistent constant (removes the spurious in-plane strain
  at build time, amortized across all candidates).
- **Richer structure design ops — holes + hydrogen + subsurface** *(feature,
  open — owner `structure` op set + `quest/tick.py` proposal rules).* Widen the
  proposer's design knobs beyond surface substitution: **remove_atom** (surface
  vacancies / holes), **add H** on the surface *and* subsurface/interstitial
  (hydride/subsurface-H chemistry), and subsurface dopant placement (not just
  adatoms). Each needs a compact op the `slab`-based proposal template can emit
  and catpath can inject.
- **struct_relax infra failures no longer launder into a dead-end verdict**
  *(bug, FIXED — owner `workers/job_types/struct_relax.py` +
  `workers/executors/{_common,ssh_node,claude_inproc}.py` + `quest/compute.py`
  harvest).* `struct_relax`'s dispatcher now stamps a `failure_class` (`"infra"`
  vs `"non-convergence"`) on every `record_failure(...)` call — the container/
  runner/executor dying (crash, OOM, malformed output, crash-loop guard,
  uncaught dispatcher exception) is `"infra"`; only a completed run whose
  relax code itself reports `ok: false` is `"non-convergence"`. `quest/compute.
  py::harvest_measures` reads it: an `"infra"` failure no longer tags
  `ruled-out:relax-failed` — it stays eligible for retry. Regression tests:
  `tests/test_struct_relax_job.py` (`test_dispatch_infra_failure_is_classed_
  infra`, updated `test_dispatch_failure_records_no_cache_row`),
  `tests/test_ssh_node_executor.py::test_poison_guard_fails_past_max_attempts`,
  `tests/test_quest_compute.py::TestHarvest` (`test_infra_relax_failure_does_
  not_rule_out_candidate`, `test_non_convergence_relax_failure_rules_out_
  candidate`). **Remaining, live-data ops action for Reto** (not done here —
  deliberately not touched by this fix): un-rule-out the already-poisoned
  prod candidate **st164913** (drop its `ruled-out:relax-failed` tag +
  correct the dossier text that called Pd(111) unstable) now that the fix is
  shipped. Also still open: fix the actual spark `struct_relax`/`gpaw-relax`
  container lane so relaxes genuinely succeed (this fix only stops a failure
  from being *misclassified* — it doesn't make the container run).

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

## 🔌 pcb / EDA (ADR 0042)

`pcb` kind shipped to main (squash `b6a749f`, migration `0047_pcb_kind.sql`) —
store ops, Pcb/Part/Datasheet handlers, jlcparts catalog, the eyes, the
delta-objective autoplacer, BOM/CPL/netlist/DSN/mechanical exporters,
Freerouting round-trip, 8 EDA skills, `[pcb]` extra.

- **v1 done-bar (orderable board) blocked on 3 deploy binaries** *(feature,
  blocked).* `pcb/footprint.py::_easyeda2kicad_fetch` raises `Unsupported`
  when the optional `easyeda2kicad` dep is absent — real EasyEDA→KiCad
  footprint conversion isn't wired anywhere yet. Also needs the Freerouting
  jar, and (Tier 2) `kicad-cli` for gerbers — none installed on any host.
- **Cluster EDA ansible role — committed, not pushed** *(ops, open — owner
  Reto, `~/work/cluster` `roles/precis_eda`).* Tier-1 only (JRE + jar +
  `PRECIS_FREEROUTING_JAR` on gateway/melchior). Three landmines inside it:
  (1) the role's Freerouting default pins **v1.9.0**, coupled to
  `pcb/route.py::_cmd`'s 1.x batch CLI (`-de in.dsn -do out.ses -mp 0`) — 2.x
  reworked the CLI, don't bump without rewriting `_cmd`; (2) the jar's
  sha256 pin is blank (supply-chain TODO); (3) unverified — the DSN emits a
  via referencing a padstack never defined in its library section, check on
  the first real-jar run.
- **Slice 3 — datasheet lazy ingest** *(feature, open).* Not started.
- **Slice 8 — web ratsnest SVG + BOM table** *(feature, open).* Not started.
- **Slice 9 — design-session orchestration (capstone)** *(feature, open).*
  Not started.

## 💰 Budget guardrails — global spend circuit breaker

Design [`docs/design/budget-guardrails.md`](docs/design/budget-guardrails.md)
(the doc's own "not built" status header is stale — Pieces B and real-cost
capture are shipped; treat it as historical design-of-record, not
present-state). **Piece B (the global circuit breaker) and real-cost capture
are SHIPPED** on `main` (confirmed 2026-07-22 against `tests/test_budget.py`):
`breaker.gate_tier` is called from `router.dispatch`
(`utils/llm/router.py:832`) and `breaker.gate_paid` from the cache fetch path
(`handlers/_cache_base.py:651`); both gate on the rolling dollar meter *or*
(for the `claude -p` OAuth transport) the subscription-quota snapshot; both
alert on trip/clear and auto-clear as the window ages; `/budget`
(`precis_web/routes/budget.py`) exposes web-editable
`PRECIS_BUDGET_HOURLY_USD`/`_DAILY_USD` overrides plus a "resume now" bypass.
Real-cost capture is also done end-to-end: Claude reports its own cost;
`result_from_openai` (`utils/llm/router.py`) prefers OpenRouter's returned
`usage.cost` over the local price-table estimate; `handlers/perplexity.py`
prefers the response's own `usage` cost block over its flat `ClassVar`
estimate. Remaining:

- **Piece A — cost-band affordance** *(feature, open — machinery only).*
  `src/precis/budget/bands.py` has the `Cost`/`Pace` enums, the tier→band
  table, and `Band.label()` (`'free · fast'` etc.) — but nothing outside
  `bands.py`/`breaker.py` imports `band_for_tier`/`is_expensive`, so the bands
  are **not actually surfaced to any model** yet (no prompt/skill references
  them). Still open: wire the label + a permissive "escalate freely when
  needful" policy line into the relevant system prompts. Owner
  `src/precis/budget/bands.py` + wherever agent system prompts are assembled.
- **Piece C — per-entity cost attribution** *(partly shipped).* `LlmRequest.ref_id`
  now stamps `llm_call_log.ref_id` (was never wired → 100% null in prod), so spend
  is attributable to an *entity*, not just a `source` pass — **cannot be
  back-filled**, so it's stamped at dispatch. Live on `quest_tick`/`quest_review`
  (+ lane-split source) and the active job-type lanes (`structure_propose`,
  `cad_propose`, `cad_discuss`, `good_search:triage`). Mining CLI: `precis llm cost
  [--days N] [--by transport|source|ref|model] [--source X]` (read-only rollup —
  calls · real-$ · char volume · wall-clock, units kept *separate*). *Remaining
  follow-ups:*
  - **Stamp the rest of the attributable callsites** — `precis_web/ask.py`
    (`generate_answer`'s `conv_ref_id` param is accepted but not threaded onto
    the `LlmRequest`) + `workers/_chase_llm.py` ×3 (`dispatch(LlmRequest(...))`
    calls carry no `ref_id` — needs threading from callers). Pass-level passes
    (dream, review) legitimately carry no single ref — leave them.
  - **Local-lane visibility** *(shipped — lite logging).* The corpus batch passes
    (`llm_summarize` / `classify` / `paper_glossary`) previously ran
    `log_call=False` (invisible). They now write a **lite** `llm_call_log` row —
    metadata (chars / cost / duration / ref_id) kept, the ~18 KB unique-per-call
    replay blob skipped (`LlmRequest.log_blobs=False`; ~660 B/row). So
    local-vs-cloud volume + wall-clock **is** mineable via `precis llm cost`.
    `route_log.gc` (90d floor, `PRECIS_LLM_LOG_RETENTION_DAYS`) is now wired into
    the sweeper (was defined-but-uncalled) since the batch passes add ~1 row/chunk.
    *Residual — non-LLM compute only:* spark DFT / relax / fold + container jobs
    never touch `dispatch`, so a placement view over those still needs its own
    counter (the factory-console §8 `service_calls` rollup: per `(pass, host, day)`
    count + wall-clock). Build only if the week's data says local *compute* (not
    LLM) capacity is the constraint.
- **Open decisions** (design doc): ledger union without double-count; per-model
  price-table source + upkeep; cheap-band threshold; real cap defaults.

## 🔒 Proprietary / local-only content routing (backlog)

*(feature, open — owner `utils/claude_agent.py`, `utils/claude_p.py`,
planner writer, reviewers).* No tag axis or routing guard exists yet for
"this content must stay local" — a corpus tag search finds nothing under
`proprietary`/`local-only`. Data-governance need: mark refs/chunks that must
never leave the box via a cloud LLM call, and have the agentic dispatch +
one-shot judges + planner + reviewers exclude tagged content from cloud
prompts, routing to a local model instead. Needs a local-model adapter peer
to the cloud transports (the ADR-0046 router's `Tier.LOCAL_*` already
exists as a landing spot) plus a guard that refuses to assemble a cloud
prompt containing any tagged ref. Pairs with per-surface persona work
(writer/chat/reviewer each with their own role + backend).

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

## 📧 `email` kind — next steps (slices 1–4 shipped)

Slices 1–4 SHIPPED to `main` (slice 4 = `inject_scan` tier-1/2 + quarantine
ladder, `cfb702f9`; dark behind `PRECIS_INJECT_SCAN_ENABLED`). Design +
present-state: `docs/design/email-kind.md`, `state-map.md` `email` bullet.

- **DEPLOY slice-4 code + ENABLE mail_poll — Reto's Phase-2 window.** Slice-4
  code is shipped but **not deployed** (dark, so harmless to lag). The
  `mail_poll` enable flag for melchior is **prepared, uncommitted** in the
  cluster working tree (`~/work/cluster`): `inventory/host_vars/melchior.yml`
  (`precis_worker_mail_poll: true`) + a `PRECIS_MAIL_POLL_ENABLED` /
  `PRECIS_INJECT_SCAN_ENABLED` gate block in
  `roles/precis_worker/templates/precis-worker.plist.j2` (mirrors
  `precis_worker_classify`). **Not deployed on purpose:** the cluster repo has
  another session's in-flight Phase-2 `precis_worker_agent` provisioning (new
  `tasks/main.yml` steps + a colima plist) that a full `scripts/deploy` would
  sweep in. Sequence with that Phase-2 deploy: a normal `scripts/deploy` picks
  up slice-4 code + the mail_poll flag together and starts polling
  `rs@retostamm.com` from melchior. (Reto's session-guard also blocked me
  committing the cluster edit; commit + deploy is yours.)
- **Enable slice-4 `inject_scan` after verifying mail_poll's tier-0 rows** —
  set `precis_worker_inject_scan: true` on melchior (gate block already added);
  it runs on the local `summarizer` proxy there. Kept dark until the tier-0
  verdicts look right in prod.
- **Slice 5 (design-only)** — opt-in promotion (`split_text`→`write_paper`-equiv
  for a chosen clean message) + wire the recurring morning brief to read clean,
  non-quarantined, summarized email rows. Send (SMTP) is a later slice behind a
  confirm-gate.

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

## 🟣 Turn-taking fisheye (ADR 0051) — Level 2 residual

Level 1 (fisheye context — policy-chosen eyes, no focus verb) shipped +
deployed + live, default-ON at both sites it applies to:
`workers/job_types/plan_tick.py` (planner) and `workers/dream_agent.py`
(dreams), via `utils/fisheye.py::render_fisheye` +
`utils/working_set_render.py::render_working_set`. Reviewers stayed
out-of-scope (they read the strategic todo-tree, not a chunk-tree — a
different render model). Level 2 (fisheye *curation*) is unbuilt:

- **`focus` verb on the MCP surface** *(feature, open).* Wire
  `workers/working_set.py`'s `WorkingSet`/`Eye` + `render_fisheye` behind an
  agent-facing verb so a model can place/remove its own eyes, not just
  planner/dreams' policy-chosen ones.
- **`--max-turns 1` render-loop driver** *(feature, open — owner
  `workers/job_types/plan_tick.py`).* Gate `PRECIS_TURN_LOOP`; the decay
  ladder + bunched eviction (`WorkingSet.crunch`) already exist but nothing
  drives a single-turn render→act→re-render cycle yet.
- **Promote-plan-node→todo** *(feature, deferred).* Needs `TodoHandler`
  `anchor=` support; belongs with the render-loop work.

## 🔵 Turn-as-job routing + context DSL *(deferred — design captured, not sliced)*

Design [`docs/proposals/turn-routing-and-context-dsl.md`](docs/proposals/turn-routing-and-context-dsl.md).
Every turn = `kind='job'`; Part 0 thread persona + cache-ordering + affinity
scheduling; Part 1 delegate-on-confidence routing; Part 2 stateful context DSL
(ADR 0036 handles + fidelity ladder). First slice = persist turn-as-job + shadow
router. Owner: `handlers/job.py` + `workers/dispatch.py` + `utils/prompt/`.

## 🔍 Paper search — `unique_per='paper'` default mode

Tier-1 broad retrieval (RRF fusion, `handlers/paper.py::PaperHandler.search`)
shipped `per_paper=N` as an opt-in diversity *cap* on fused results —
useful for breadth-triage but not the resolved design below. Default is
still chunk-rows.

- **Paper-row default mode** *(feature, open — design resolved
  2026-06-03, unbuilt).* Make `unique_per='paper'` (one row per paper: best
  handle + `more` count of additional hits + best-chunk's own keywords) the
  default; `unique_per='chunk'` (today's shape) becomes the opt-in/drill
  mode, implicit when `scope=` is set. Mode-aware page sizes (`top_k=25`
  paper mode / `10` chunk mode) + a top-line "N papers of M matched (K chunk
  hits)" counter + "refine before paging" guidance in `precis-search-help`
  ship with it. Known edge from review: with `per_paper=1` a `card_combined`
  chunk can consume a paper's only slot before body-chunk dedup runs.

## 🟡 Unified item view (`/items`)

Slices 1–3a shipped + deployed (cross-kind search page + reading-intent flags +
`Store.search_chunks_across_kinds`). Rest of slice 3 SHIPPED: `ItemPresenter`
grew the full method contract (`preview`/`hover_preview`/`thumbnail`/`actions`,
generic defaults + a `youtube` thumbnail override), result pagination
(`page=` past the 30-item cap, threaded through `search_chunks_across_kinds`
and `recent_refs`), an author/source kind facet (`role='artifact'` chips
alongside the source chips), a folder facet (`Store.list_folders` +
`parent_id` narrowing on the no-query landing), and per-row thumbnails +
hover popovers in the template. Design
[`docs/proposals/unified-item-view.md`](docs/proposals/unified-item-view.md).
Owner `precis_web/routes/items.py`, `precis_web/item_view.py`.

- **`@abstractmethod` promotion** *(open).* The presenter contract has a
  generic default for every method; flipping to the check-time-totality
  guarantee (the design doc's acceptance criterion) needs a dedicated
  presenter per source/artifact kind (~40 kinds) — a separate, larger pass,
  not a mechanical follow-on. Do this alongside (not instead of) the
  kind-taxonomy audit below since both touch every kind's declaration.
- **Legacy-route retirement — investigated, none are a clean 1:1** *(open,
  each individually scoped)*. `/items` stayed additive; none of the five
  reduce to a filter-preset without losing real functionality:
  - `/drive` — folder CRUD (create/rename/move/delete) + child-count tree
    nav; `/items`' folder facet is read-only browse, no mutation surface.
  - `/papers-needed` — the watch-dir dropzone paths/descriptions (page-level,
    not per-row) and the second `acquire` flag axis (`cant-get-uol` /
    `is-book` / …) have no `/items` equivalent.
  - `/papers/triage` — per-row quick actions (`✓ Clear flag`, `🗑 Delete`)
    that `/items`' `actions()` seam is wired for but nothing populates yet.
  - `/tags/refs` — shows soft-deleted rows and arbitrary kinds (`job`,
    `conv`, …) with no presenter, both by design invisible on `/items`.
  - `/refs` (consolidated) — same non-item-kind reach as `/tags/refs`
    (memory/conv/gripe/todo/job), plus its own detail route
    (`/refs/{kind}/{id}`) is `/items`' `open_url` default and must stay.
  Once `actions()` grows a real "clear flag" / "delete" implementation
  (coupled to the abstractmethod pass above, since that's where per-kind
  actions get wired), re-check `/papers/triage` — it's the closest to
  clean.
- **Kind-taxonomy audit** *(open, coupled).* Reconcile `role`/`corpus_role` drift
  (datasheet, pres); collapse near-dup kinds (perplexity-*/websearch/web/wikipedia;
  calc/math/oracle); rewrite `precis-*-help`. No-legacy-alias license.
- **Slice 4 — "write a document from this view"** *(open).* A tailored filter is
  a serialized query → mint an authoring job scoped to exactly those refs.

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

## 📝 Draft footnotes + annotations (deferred design)

Authors slice shipped (`refs.authors` byline + ROR affiliation, LaTeX/docx
export, web edit form — mirrors papers, no new kind). Two siblings from the
same design split are still deferred, unbuilt:

- **Footnotes** *(feature, deferred).* A first-class `footnote` chunk_kind
  anchored to its block via `meta.anchor`, out-of-flow, embedded+citable,
  ships in export — parallels `term`/`figure`/`caption`.
- **Annotations** *(feature, deferred).* A separate editorial layer, NOT in
  `reading_order`; `draft_annotation` chunk_kind + `meta.anchor` +
  `meta.author`, append-only via `chunk_events` (the `gripe_comment`
  idiom), does not export.

## 📓 reMarkable send — device pairing pending

Send-draft-to-reMarkable-2 shipped + deployed (render footnote-cite
excerpts, container uploader via `ddvk/rmapi`, job `remarkable_send`, web
+ CLI entry points). Runs **dark** — the button stays hidden and the job
declines until S0-ops device pairing happens:

- **Pair + arm** *(ops, open — owner Reto, `docker/remarkable/README.md` +
  `deploy/roles/remarkable`).* `rmapi` device pairing (8-char code) →
  vault `REMARKABLE_RMAPI_CONFIG` → `ansible-playbook playbooks/47-
  remarkable.yml` → set `PRECIS_REMARKABLE_IMAGE` in `precis_shared_env` +
  re-run the agent-worker role. First build has 3 unverified externals
  (exact `ddvk/rmapi` release asset names, the `rmapi.conf` format, colima
  bind-mount sharing on macOS) — check at first run, not blind-trust.

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

## 🔧 Autonomous fixer loop (ADR 0048) — residuals

`src/precis/fixer/` — the repo-dev CI scheduler (tick a `docs/proposals/`
proposal or open gripe → headless `claude -p` in a worktree → gate →
report/ship/deploy, wraps `/go`) is **built + shipped + running live** on
Reto's laptop (`com.precis.fixer` LaunchAgent, hephaestus, report mode,
20-min interval, dodges the redeploy-restarts-itself problem by not being a
deploy target). Dial: `PRECIS_FIXER_AUTONOMY` = report/ship/full (full-auto
ship+deploy proven end-to-end).

- **Gripe 49958 — NEEDS_YOU discards a salvageable build** *(bug, open —
  owner `fixer/tick.py::run_tick`).* On a real gate failure (mypy,
  non-auto-fixable lint, non-zero `claude -p` exit) the branch is never
  pushed and the `finally` removes the worktree — an expensive opus build
  that's 90% right is thrown away with nothing to inspect. Proposal:
  push-on-NEEDS_YOU too (pair with branch GC so half-built branches don't
  accumulate), or keep the failing worktree under `.fixer-work/` with a
  pointer in the report.
- **Stale branch cleanup** *(polish, open — needs Reto's OK).* `fix/smoke`,
  `fix/build-prompt-map-freshness`, `fix/fixer-persistent-log`,
  `fix/launchd-smoke` (origin) + `fix/shippath` (local).
- **`PRECIS_FIXER_DISCORD_WEBHOOK` unset** *(ops, open).* Loud NEEDS_YOU
  reports are log-only (`/tmp/precis-fixer.log`), not proactively surfaced.
- **Agentic post-deploy followup is a `/readyz` stub** *(feature, deferred).*
  Real look-at-prod-and-fix-forward is just the next review-gated proposal
  today, not an active post-deploy check.
- **Deferred (ADR-filed):** groomer write-side (the `whatneedsdoing` half),
  automated `ready`-on-gripes, a doc-freshness ship judge, a durable
  `agentlog` record per tick, `sandbox_run` job-type isolation.

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
- **Rationalize the cluster daemon-user model** *(ops, open, deferred — owner
  `~/work/cluster`, not urgent).* `hermes` (OAuth/`~/.claude` state) vs
  `deploy` (owns `/opt/homebrew` + the colima docker socket) is a two-user
  split that already bit the Phase-2 container cutover once (hermes
  couldn't reach deploy's 0600 docker socket on melchior). The melchior
  instance was worked around via a run-as cutover; the fleet-wide question
  — how many daemon users, what each runs, per host — is still open. Scope
  properly once Phase-2 settles; likely fold hermes→deploy or land on one
  `precis` service account.

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

## 🏷️ `OPEN`-namespace teardown *(design, awaiting Reto's review)*

Design [`docs/design/open-namespace-teardown.md`](docs/design/open-namespace-teardown.md)
(recovered to main 2026-07-19 from a dangling commit; status: design). The
free-form `OPEN` tag namespace conflates three things (machine control
plane, ADR-0047 curated-axis staging, folksonomy) across ~45 prefixes, 52%
singletons. Not implemented — the doc is the full spec (three piles:
**MACHINE** — ~20 deterministic prefixes to migrate to real axes/columns;
**CONSOLIDATE** — `topic:`/`interest:` (~2000 rows) into a curated axis via
the ADR-0047 minting lifecycle; **DELETE** — junk prefixes) + a migration
table + the exact-match cull rule (`namespace='OPEN' AND value LIKE 'p:%'`,
never `namespace LIKE 'OPEN%'` — that eats the ADR-0047 `OPEN-QUESTION`
axis). Blocks the OA-acquisition roadmap's §G `referenced_works`→topics
wiring (above). *(design-review, open — owner: whoever reviews the doc's
open questions with Reto: `level:` axis-vs-column, `internal-thought`
dual-writer, `sticky:` fate.)*

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

## 🔵 Small backlog asks

- **Stateless `time`/`date` handler** *(feature, open — owner
  `handlers/`).* No `time`/`date`/`clock` kind exists (`handlers/calc.py`
  is the only stateless kind today). Mirror `calc.py`'s shape (`KindSpec` +
  a `get` verb, no DB/embedder): `get(kind='time')` → now UTC+local,
  `get(kind='time', id=<ts>)` → parse/format/convert. `units`
  (conversions) and `regex` (test/match/extract) are sibling candidates,
  same template.
- **Per-tool-call ledger** *(feature, open — owner `runtime.py`).* Today's
  telemetry (`agentlog`, `ref_events`, job chunks, worker logs) has no
  per-tool-call row, so "which verb/kind/arg-shape confuses agents" isn't
  queryable. Proposed: a `tool_calls` table (sibling of `ref_events`/
  `alert`; numeric, not embedded — `call_id, ts, agentlog_id, source,
  verb, kind, arg_shape jsonb, outcome, error_type, result_count,
  latency_ms`) written from the verb-dispatch chokepoint in `runtime.py`.
  Feeds an `error-rate GROUP BY (verb,kind)` MCP-improvement backlog; a
  nursery friction-detector could auto-file a gripe past a threshold.
- **Universal short codes** *(design, deferred).* ADR 0032's base-62
  `chunk_id` encoding (manuscript-only, `5BL5`-style) hasn't been promoted
  beyond that one kind — no `base62` helper exists outside it. Verdict was
  additive-not-replacement (coexist with meaningful handles for top-level
  refs); prove on manuscript chunks first, promote in a later ADR only if
  it earns its keep.

## ⏸️ Snoozed — blocked upstream

- **Dependabot #44 — `transformers` <5.3.0 RCE (high).** `Recheck-after:
  2026-08-01`. `Unblock-when:` `marker-pdf` drops its `transformers<5.0.0` cap.
  Today every `marker-pdf` (≤1.10.2) pins `transformers<5.0.0` and precis needs
  marker (`[paper]`), so `>=5.3.0` is unsatisfiable as a lockfile bump alone.
  Tolerable: exploit surface ~nil (precis only loads the trusted bge-m3 embedder,
  never a user model path or `trust_remote_code`). **Recheck:** re-run `uv lock
  --upgrade-package transformers`; if it reaches ≥5.3.0 take the fix + validate a
  sample re-embed for cosine drift; else bump `Recheck-after` +2 weeks.
  **Re-verified 2026-07-18 (still blocked):** PyPI shows `marker-pdf` latest is
  still `1.10.2` (no new release), capping `transformers<5.0.0`. Note a *second*
  lock has appeared — `surya-ocr` moved to `0.22.0` requiring `transformers>=5.12.1`,
  but marker also caps `surya-ocr<0.18.0`, so the newer surya can't be used either.
  Both locks release only when marker-pdf ships a version that lifts them. → +2wk.

- **Dependabot #45 — `transformers` LightGlue-load RCE (high).** `Recheck-after:
  2026-08-01`. `Unblock-when:` same block as #44 — `marker-pdf` (≤1.10.2) caps
  `transformers<5.0.0`, so the fixed `transformers` is unsatisfiable as a lockfile
  bump while precis needs marker (`[paper]`). Exploit surface ~nil: the RCE is in
  the LightGlue model-init path, which precis never loads (only the trusted bge-m3
  embedder; no `trust_remote_code`, no user model path). **Recheck together with
  #44** — one `uv lock --upgrade-package transformers` clears both when marker lifts
  the cap; else bump `Recheck-after` +2 weeks.

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

**Markup-first ingest (separate feature, `ingest/markup.py`) — decide the
PDF-race before flipping the flag** *(design-review, open — owner
`workers/fetch_oa.py::_run_markup_cascade`/`_markup_fetch_enabled`).*
JATS/HTML/LaTeX-before-PDF+OCR ships dark behind `PRECIS_FETCH_MARKUP`
(still default-off). Per-stub, the markup pass runs first (best-effort,
swallows its own errors) then the PDF cascade runs unconditionally after —
the live-drop ordering between the two hasn't been decided (which body
wins when both succeed). Decide before enabling on any host.

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
- **Memory currency-auditor → own pip? 1-month check-in** *(feature, deferred
  — decide by 2026-08-19; owner `scripts/memory-lint`).* Shipped
  `scripts/memory-lint --currency`: treats each memory as falsifiable anchors
  (gone kebab branch/worktree naming unshipped work · repo path missing on main)
  and runs the exact git+fs oracle, so the once/day reconsolidation pass gets a
  suspect punch-list instead of re-reading every file (git+fs only — gripe-status
  / deployed-sha oracles need the prod MCP, stay in the judgment pass). Prior-art
  scan (`perplexity-research:164887`) found **no** open-source Claude-Code memory
  tool that verifies memories against repo ground truth — claude-mem (74.8k⭐),
  MCP `server-memory`, Mem0/Zep/Letta, memsearch all store/compress/retrieve, none
  audit; the repo-dev-toolkit half (worktree ship, doc-guardian orphan-docs, `rtk`
  itself, awesome-claude-code) is a crowded commodity. So the *only* novel slice is
  this auditor. **Decision to make ~2026-08-19:** after a month of our own use, is
  it worth extracting as a standalone pip/plugin (genericize oracles off precis
  coupling, own maintenance), or does it stay a repo-local script + a line in
  `docs/how-to-setup-like-this.md`? Prior is **transient at best** — the recipe
  doc is likely the right home; only extract if the month proves recurring value.
- **Repo-dev hooks — 2 deferred** *(feature, deferred — marginal).* The tier-1
  guards (PROD-write / sealed-migration / git-stash), the map-staleness extension
  (ADR + skill triggers + `migration-check` at write), the PreCompact
  persist-residuals reminder, and `session-size-nudge` (propose `/compact` at
  transcript-size tiers) all SHIPPED. Deferred as low-value / noise-risk, build
  only if the pain shows up: bare-`pytest`→`scripts/test` nudge;
  Stop-with-dirty-worktree reminder.
- **Mutation testing via `cosmic-ray`** *(polish, blocked-on-adoption — owner
  `pyproject.toml` + nightly).* `mutmut` is incompatible with our `-n auto`;
  `cosmic-ray` runs the test command as a subprocess so `pytest -n0` works. Scope
  to one pure-logic module (SSRF guard), nightly.
- **`subsystem-analyst` (opus) agent** *(feature, conditional — owner
  `.claude/agents/`).* A deep "how does the whole X work" synthesis subagent —
  build ONLY if the haiku `navigator` proves too shallow. Don't pre-build.
- **Test-suite setup tax — serialized per-worker template clones** *(polish,
  open — owner `tests/conftest.py::_initialise_test_db`).* Profiling
  (`--durations`) shows the suite is **setup-dominated**: ~340 s of fixture
  setup vs ~120 s of actual test-logic (7774 tests, ~100 s wall @ `-n6`). After
  the leak fix, the dominant remaining cost is the **6 per-worker `FILE_COPY`
  template clones, fully serialized under the session advisory lock** (the
  76/50/30/15 s "setup" tail — the last worker waits behind all prior clones).
  Options, none free: cap gate workers (fewer clones — already `-n6` not
  `-n auto`); shrink the template (lighter clone); or let clones proceed with
  less lock overlap. Real correctness/speed tradeoff — measure before touching.
  The per-test TRUNCATE base (~40 ms × ~3000 DB tests ≈ 128 s CPU / ~21 s wall)
  is the other aggregate; TRUNCATE is already the cheap isolation choice.
  No coverage is measured anywhere (no `pytest-cov`/`--cov`) — a separate gap.

---

_Last compacted 2026-07-18: removed all done/shipped entries (history in
`git log`), condensed open items. Prior detail is recoverable from git._

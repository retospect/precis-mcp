---
id: precis-job-help
family: work
title: precis — offline work, addressable
summary: offline-work substrate — submit, poll, cancel; parent-todo contract, status, event timeline
answers:
  - how do I submit an offline job tied to a todo?
  - how do jobs differ from a regular synchronous tool call?
  - how do I check what jobs are currently running or queued?
  - how do I cancel a job that's taking too long?
  - how do I submit a job idempotently so a retry doesn't double-run it?
  - how do I read worker_logs / what is the cluster doing right now?
  - why is my todo parked child-failed (child-failed-parked, child-failed-final) and how do I unpark it?
applies-to: get/search/put/tag (kind='job')
tags: workflow, verbs, troubleshooting
kinds: job, todo
status: active
---

# precis-job-help — submit a job, poll for status, cancel

A **job** in precis is **one execution attempt** of an intent.
The intent itself lives as a `kind='todo'` ref; the job is its
child via `parent_id`. Each job has a numeric id, a `STATUS:` tag,
a parent todo, an optional `link` to whatever it operates on
(e.g. a gripe), and a comment timeline of `job_event` /
`job_summary` chunks.

Submit. Walk away. Come back to a `STATUS:succeeded` row — the
parent todo's `meta.auto_check={'type':'child_job_succeeded'}`
will resolve it to `STATUS:done` on the next auto_check pass.

Not cron. Not celery. Not a subprocess you wait on.

## The contract every job submit must satisfy

* **Every new job must declare its parent todo.** `put(kind='job',
  parent_id=<todo_id>, ...)` is the only legal shape. The handler
  rejects orphan submits with a clear next hint.
* **The canonical path is `meta.executor` on a todo + the
  dispatch worker.** Write the intent as a todo with
  `meta={'executor': ..., 'job_type': ...}`; the dispatch worker
  mints the job under it. Direct `put(kind='job', parent_id=N,
  ...)` works for ad-hoc submits but skips the auto_check
  injection — see `precis-minter-help` for the full pattern.
* **A failed job tags its parent — with bounded self-healing.**
  Infra-class failures (`swept:claim-orphaned`, `infra:child-killed`
  — lease orphan / compute child died by signal or without a result
  file) do NOT park the parent: the dispatcher re-mints a fresh
  attempt, capped (3 per 6 h). Content-class failures tag the parent
  `child-failed:<job_id>`; the sweeper's `unpark` phase then retries
  it autonomously on an escalating cool-down (12 h / 24 h / 48 h,
  cap 3) before latching the terminal `child-failed-final` tag.
  Transient causes retry sooner: a failure reason reading as a
  rate/spend limit or transient API fault stamps `meta.retry_after`
  on the job, and the unpark fires at that time (15 min–2 h) instead
  of the 12 h base — same cap. `child-failed-final` —
  only *that* tag means the substrate has given up and a human (or
  the parent's owner) must decide. The nursery surfaces
  still-recoverable parks per-leaf and finals as one aggregate.
  A terminally parked leaf carries BOTH `child-failed-final` AND its
  last open `child-failed:<job_id>` tag; clearing them takes the
  retry verb or a single two-tag `tag(remove=…)` (see below) —
  removing `child-failed-final` on its own does not stick.

## Why is my todo parked child-failed (child-failed-parked / child-failed-final)?

A content-class job failure tags the parent todo `child-failed:<job_id>`
(the "child-failed-parked" state). The sweeper's `unpark` phase retries it
on a 12 h / 24 h / 48 h cool-down, cap 3; only `child-failed-final` means
the substrate gave up. The full rules are the "A failed job tags its
parent" bullet above; the manual retry verb is under "Retry a failed job"
below. Infra-class failures (`swept:claim-orphaned`, `infra:child-killed`)
never park the parent — a fresh attempt is minted instead.

## What is a job in precis
## How do jobs differ from regular tool calls?

A unit of offline work, addressable by id. Lives in the DB; runs
on whichever host has a runner for its executor. Reports back via
status tags + a `job_summary` chunk that you can search later.

Use a job when:

- The work takes minutes-to-hours.
- You want to come back to it later or hand off.
- Another agent or process needs to find or check it.
- It needs to run on different hardware (cluster, GPU box) — once
  more executors land.

Don't use a job for work that fits inside the current conversation.

## What job types are available?
## List the registered job_types

Agent and internal job types; the compute and sandbox types are in the
next section.

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

## Fleet and dark-factory job_types — queue drains, slices, plan dispatch

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

## Dark-factory job types — the lanes that run slices

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `run_slice`         | `ssh_node`      | Launch a slice todo through its enabled lane's adapter (`claude_p`, `codex_exec`, `devin_tree`) in worktree `slice-td<id>`, branch `slice/<id>` from `origin/main` (from `origin/release/r<N>` when the todo's `meta.land_target` or a `land target: release/r<N>` brief line names one). No DB credentials; only Claude gets precis MCP. Codex and Devin record everything in their final message, posted as `handoff:`. Devin refuses explicit models outside the lane's list; unpinned vendor lanes without a default pass no model flag. Job metadata records `lane`/`adapter`/`model`/`effort`. Every branch is checked against `deny_write_paths` (`STRICT_DENY` — FACTORY plus the human-only set — or `HUMAN_ONLY_PATHS + INTERIM_DENY` while the DB-only `factory.deny_relaxed` setting is on); a violation fails. Clean exits with commits mint `land_slice`; failures reopen the todo with `attempt:<n>`. Generator roles: `reviewer` posts `review:`, `analyst` posts `analysis:`, others create child todos. Params `todo_id`, optional `lane`, `model`, `effort`, `head_sha`, `target_node`. Kill switch recovery: ask an operator (`docs/runbooks/dark-factory-arming.md`). Host capabilities remain `repo_checkout`, `claude_bin`, `git`; missing capabilities leave the job queued with a scheduler alert |
| `land_slice`        | `ssh_node`      | Dark factory lander, minted by `run_slice` (one per slice, idem key `land_slice:td<id>`): `scripts/ship --quick "<the slice's commit subject>"` in the slice worktree, no LLM. A todo with a land target (`meta.land_target = release/r<N>`, a release fix) ships `--quick --onto release/r<N>` instead: the change lands on that release branch only and main does not move. Green → `meta.landed_sha`, a `landed: <sha>` comment + `STATUS:done` on the todo when the ship's land-notify did not write them, the sha written into the plan's pending `sha-in-prod` gate, then the worktree and `slice/<id>` branch (local and origin) deleted. Red (lint ✖, merge conflict) → the job fails with the ✖ line, a `handoff:` comment names it, the todo returns to `STATUS:open` with `attempt:<n>` and the branch stays for the next build. With `pr: <n>` (a `pr-intake` plan's land node, `checked: true`) it lands the PR head on `slice/pr-<n>` with `--checked` and closes the PR with a comment naming the squash sha. Params `todo_id` plus `worktree` + `branch` (a slice) or `pr`; optional `checked` (`true` selects `--checked`; default and `false` are `--quick`; `run_slice` mints it when the worker committed the tree it was left or the last `tests:` marker in its final message is not `ran`), `lane`, `target_node`. Security boundary: `src/precis/plans/boundary.py` registers the path sets and added-line rules (`boundary_hits`) whose diff routes the land through the `security-review` lane — read-only today, nothing calls it yet (td574934 A2). Needs host capabilities `repo_checkout`, `git` (pinned to the host that ran the slice) |

## Dark-factory plan passes — plan_dispatch and factory_rescue_tick

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `plan_dispatch`     | `job_inproc`    | Dark factory: one pass over every plan subtree (`plan:<id>` tags, gate/loop/generate/auto_close nodes) — evaluate gates, re-mint loop bodies, close finished phases, mint one job per doable task (`meta.op`, default `run_slice`) in (prio ascending, id) order across every plan root — a node without a prio takes its nearest ancestor's, default 5 — act on a reviewer node's `review: LAND` / `review: FIX — <list>` / `review: REVERT — <why>` comment (close it; mint a fix task; mint a revert task and reopen the build), release claims older than 4 h, and park a task at its retry cap — `meta.retries` wins, else the lane-ladder length (2 for a model-pinned or ladder-less op) — into rescue; an `ask-user` + `showstopper` todo only on a human-only hit, no enabled lane, or a rescue's own ask. Params: optional `dry_run`. Read-only while the factory kill switch is on or a `service_config` `plan_dispatch` row has prio 0. Armed by one recurring todo — ask a human operator (recipe: `docs/runbooks/dark-factory-arming.md`). Needs no host capability; notes each plan job queued over 10 min that no host can place |
| `factory_rescue_tick` | `job_inproc`  | Dark-factory stall watchdog: one DB-only pass judges every dispatchable plan node and `sha-in-prod` gate against its stage deadline (idle 20 min, queued 30 min, build 5 h, land 6 h, review 45 min, gate 72 h) — a node silent past it goes to RESCUE (one `rescue:<class>` tag, one gripe per failure signature): cancelled newest job, never dispatched, `post-land-write` (pending-sha gate after the build landed), `terminal-root`, lane no-fit. Holds (`waiting-for:`, `halt`, paused, open ask, `rescue:`, live `blocked-by`), parked nodes (fit lanes full/backed off) and `release-pass` plans are exempt. Also the `tick-silent` heartbeat: no succeeded `plan_dispatch` job in 30 min rescues its recurring todo once. Then it runs the diagnose pass: a node in rescue gets one `claude-sub` diagnose task — `rescue: REARM` re-arms it once with a corrected brief, anything else files one `ask-user` todo blocking it — except a node under a closed ancestor (`terminal-root`, or a `won't-do` up its parent chain), which closes `STATUS:won't-do` with one note and no ask (gr576258). Params: optional `dry_run`. Armed by one recurring todo — ask a human operator (recipe: `docs/runbooks/dark-factory-arming.md`). Read-only while the factory kill switch is on or a `service_config` `plan_dispatch` row has prio 0. Needs no host capability |

## Dark-factory gripe passes — ci_watch and gripe_validate

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `ci_watch`          | `job_inproc`    | CI watcher: each completed check.yml run is judged once — a red `event=schedule` nightly on main or a red `release/**` run carrying a `soak` job files one gripe per failure signature (`ci:check.yml:<file>::<test>` parametrise-stripped, `ci:check.yml:infra:<job>` tagged `fix:infra` for a node-less failed job); an open `nightly red: <sig>`/`soak red: <sig>` gripe gets a `seen again:` comment, a terminal one refiles (under 7 days it names the old handle), an open `ci:mute` tag silences it, and an unresolved gripe with no live todo link is flake-closed after two consecutive green runs while a `resolved:` sha gets `green on <url>` once a green head contains it. Params: optional `dry_run`, `repo`, `lookback_runs`. Armed by one recurring todo — ask a human operator (recipe: `docs/runbooks/dark-factory-arming.md`). Read-only while the factory kill switch is on. Needs no host capability |
| `gripe_validate`    | `job_inproc`    | Gripe ladder, last rung: close every `STATUS:verify` gripe whose `resolved: <sha>` comment names a sha in the recorded prod head (head or landed) as `STATUS:done`; one with no sha, or a sha not yet deployed, stays. Params: optional `dry_run`. Armed by one recurring todo — ask a human operator to arm or disarm it (recipe: `docs/runbooks/dark-factory-arming.md`). Needs no host capability |

## Lane health — the hourly credential probe (gr588221)

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `lane_health`       | `ssh_node`      | Lane credential probe, minted hourly by the scheduler cadence while any lane is enabled: one cheap auth check per enabled lane on a launchable adapter, run as a detached child of the claiming slice worker so it probes the host the sessions run on — `claude_p`: a one-word `claude -p` ping under the slice env (the vault OAuth path); `devin_tree`/`codex_exec`: the CLI's login status; `kimi_tree`: the membership endpoint (only while the kimi account row is enabled). A red (`auth`/`outage`) probe parks the lane out of dispatch on `parked_reason` — skipped exactly like `enabled=False`; a green probe on a parked lane clears the park and resolves its `lane-parked:<lane>` alert (a structural `auth:cli-missing`/`auth:http-*` park needs two consecutive greens); `unprobed` (CLI absent or unjudgeable) moves the stamp only; red on parked only moves the health stamp; a lane still parked past 6 h files the deduped alert. The `claude_p` probe line adds `token=present|absent` + class (`oauth-setup`/`other`, never the value) and the CLI's `result` wording; an `auth` read on an absent token applies `unprobed`/`vault-miss` — summary `auth (token absent)` vs `auth (token rejected)` (gr601062). `enabled` stays the human switch — nothing here writes it. Needs host capabilities `repo_checkout`, `claude_bin`, `git` |

## Boundary probes — the mechanical branch fence (td574934 A4)

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `boundary_probe`    | `ssh_node`      | Mechanical fence for a slice branch (no LLM, no store writes): for each boundary the branch diff hits (`envelope`, `slice_env`, `safe_fetch`, `deny_list` — a diff touching the boundary's paths), it runs `origin/main`'s `scripts/probes/<boundary>.py` inside the slice's test container against the branch tree mounted read-only (`--network none`, no DSN, no HOME mount), plus the branch's version when the diff changes or adds the probe (both must pass; a deleted probe is cannot-run). Probe contract: exit 0 pass, 1 breach with the finding on stdout, anything else or a 600 s timeout cannot-run. `meta.probes` records `{boundary, probe, probe_sha, head_sha, exit, class}` per run; a boundary with no registered probe is refused `unprobed`; anything but all-pass fails the job. Params: `worktree`, optional `branch`, `target_node`. Needs the worktree's host (`repo_checkout`, `git`, the `precis-dev` image) |

## Dark release-pass job types — the cut/poll/deploy lanes

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `release_cut`       | `ssh_node`      | Dark release pass, the `cut` node of a `release-pass` plan: `scripts/release --cut-only` in the repo checkout cuts `release/r<N>` from main and returns without waiting for CI (first superseding a stale local `round.json` — `release.round` says `confirmed`); the job stamps `meta.release = {rN, sha, range, repo}` on the plan root and the real range on the plan's `tests-green` gate and loop. No `RELEASE_CUT` line (already live, a release in flight elsewhere) fails the job. Params: optional `todo_id`, `target_node`. Refused while the factory kill switch is on, or while a reviewer on an engine-stamped `land_slice` job is open or its FIX is unlanded (the engine-review gate; `release_poll` runs the same check before minting). Needs host capabilities `repo_checkout`, `git` |
| `ci_verdict_poll`   | `job_inproc`    | Dark release pass, the `poll` node: reads check.yml's GitHub Actions run on the cut's sha (`api.github.com`, `GH_TOKEN`, else the factory's vault token) and records green/red in `release.ci_verdicts`, which the `tests-green` gate reads; while CI runs the job parks itself (`STATUS:waiting_time`) and re-polls every 10 min. With `restamp: true` (the release-fix loop's `restamp` node, after the fix nodes) it reads no verdict: it moves `meta.release` onto `release/r<N>`'s current head (sha, range end, the gate and the loop's `until`; no cut), so the next poll and digest key on the fixed head. Params: `todo_id` (reads the plan root's `meta.release`) or `sha` + optional `range`; optional `repo` (else `release.repo`, else the checkout's origin), `restamp`. Needs no host capability |
| `release_deploy`    | `ssh_node`      | Dark release pass, the `deploy` node: `scripts/release --deploy-only r<N>` deploys the plan's cut pinned and records `release.prod_head`. Refused unless the approval ask blocking the node is answered `deploy r<N>` for that cut — Reto's answer, or the tick's auto-answer under `release.auto_deploy` (exact-sha green CI + an open `release.round` on that head). Params: `todo_id`, optional `target_node`. Refused while the factory kill switch is on. Needs host capabilities `repo_checkout`, `git` (pinned to the release owner's host) |

## Dark release pass — the confirm and merge-back lane

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `release_confirm`   | `ssh_node`      | Dark release pass, the `confirm` node after `deploy` (`retries: 0`): attests the deployed runtime — every live worker's heartbeat `code_sha`, read from the store with no network, plus the web's `X-Precis-Boot-Sha` header when the `release.health_url` operator setting names its `/healthz` (unset means the web is not attested; the workers decide). On pass it runs `scripts/round deploy --confirm-runtime <sha> --runtime-evidence "<text>"` then `scripts/round close` and stamps the evidence on the plan root and `release.round`; a failed close still succeeds (the release is already `deployed/r<N>`). Any evidence mismatch or gap fails the job before any script runs and the tick's retry-cap ask goes to a human — no rollback, no `good/r<N>` tag. With `merge_back: true` (the pass's `merge_back` node, also after `deploy`) it instead merges `origin/release/r<N>` into main as `Merge release/r<N> fix forward` and pushes main with the factory credential: no commit past the cut is a no-op; a real conflict pushes nothing and mints one resolver slice (lands on main normally) plus a second `merge_back` node behind it with `strategy: "ours"` and `resolver: <todo id>`, whose `git merge -s ours` records the release head as an ancestor of main once the resolver landed; the pass root stays open until both close. Params: `todo_id`, optional `target_node`, `merge_back`, `strategy`, `resolver`. Refused while the factory kill switch is on. Needs host capabilities `repo_checkout`, `git` (pinned to the release owner's host) |

## Dark release-fix loop — the digest and rerun lanes

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `ci_red_digest`     | `ssh_node`      | Dark release pass, the `digest` node of the `release-fix` loop body: `scripts/ci-failed <run_id>` digests the newest red check.yml run on the cut's sha and posts it — secret-masked, capped — as one `brief:` comment on each `run_slice` todo `blocked-by` the digest node (the fixer's brief). A digest whose failing ids parse complete is split by the failing `src` module its assertion or import line names, else by test file (at most 8 clusters): one fix node per cluster, the extra ones minted beside the iteration's own, each landing on `release/r<N>`. A test red of 1-2 ids that passes the hard-stop lists may instead be waived (`release.red_waiver`, default `on`): a `broken-build` gripe, the verdict recorded green-waived, no fix session. With `params.probe` + `params.run_id` instead runs `scripts/ci-failed <run_id> --probe` and reports the `probe: status=<code> accepted-permissions=<value|absent> run=<id>` line — what the factory's GitHub credential may read on the run's logs endpoint; submitted by hand when the digest comes back useless (td573240). Params: `todo_id`, optional `probe`, `run_id`, `target_node`. Refused while the factory kill switch is on. Needs host capabilities `repo_checkout`, `git` (pinned to the release owner's host) |
| `ci_rerun`          | `ssh_node`      | Dark release pass, the `validated:` non-repro path (gr588028): a release-fix session ending `validated:` with no commit parks the fix node on `waiting-for:ci-rerun` and the tick mints this job — `scripts/ci-rerun <run_id>` re-fires the newest red check.yml run's failed jobs on the cut's sha, once per run id (a second `validated:` on the same run falls to the `no_commit` path); exit 0 settles ~5 min for GitHub to flip the run to in_progress, then the plan's own `ci_verdict_poll` records the rerun's verdict. A 403 fails naming `scripts/ci-rerun --probe` — the rerun endpoint needs Actions write where the digest needed only read. Params: `todo_id`, optional `target_node` (a supplied `run_id` is refused — only `--probe` takes one). Refused while the factory kill switch is on. Needs host capabilities `repo_checkout`, `git` (pinned to the release owner's host) |

## Dark release-pass starter — release_poll and the stuck watchdog

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `release_poll`      | `job_inproc`    | Release loop starter + observation-only stuck watchdog: first, at any hour, it judges the `release.round` row's `state_at` against the `release.stage_deadlines` JSON (per-stage minutes; a stage absent/non-integer/`<= 0` unwatched, an unparsable value watches nothing) and files at most one `ask-user` ruling todo per `(round, stage)` (tag `release-stuck:r<N>:<stage>`) naming only the hand verbs — deploys, supersedes and cuts stay human. Then mints one `release-pass` plan only when its gates pass — the clock is outside `release.no_release_windows` (comma-separated UTC `HH:MM-HH:MM` pairs, half-open `[start, end)`, a start later than the end crosses midnight; default `03:00-04:20`, the nightly backup window), no `release-pass`-tagged todo is open, the shared `release.round` record names no open round (absent is "no round", unreadable blocks), GitHub `compare/prod...main` reports `ahead_by > 0` with `behind_by == 0` (`diverged`/`behind` never mints), no `release/r<N>` head is in flight (in flight unless its `deployed/r<N>` tag exists AND prod contains the head — or the head is already on main, a stale superseded ref), and no `release-main:<sha12>`-tagged root already claims main's head — the claim check and the mint are one transaction under an advisory lock, so a new pass needs main to move. Else it writes one `blocked — <why>` line and mints nothing. Params: optional `dry_run`, `repo`. Armed by one recurring todo — ask a human operator (recipe: `docs/runbooks/dark-factory-arming.md`; check for an untagged pre-tag open pass root before the first arm). Needs no host capability; the kill switch covers the watchdog's ask write (it files nothing, only a `would file` note) — the mint itself is not covered: it only mints, the plan runs when `plan_dispatch` is armed |

## Inbound pull-request job types — the pr-intake plan

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `pr_poll`           | `job_inproc`    | Inbound pull requests: lists the repo's open PRs (`api.github.com`, `GH_TOKEN`, else the factory's vault token) and mints one `pr-intake` plan, tagged `pr:<n>`, per non-draft PR that has none, plus the head it was minted on as `pr-head:<sha12>` (a PR with an open plan, or a closed one on the same head, is skipped; a head that moved after its plan closed gets a new plan). The class is decided in code: `harness` (touches `scripts/ship`, `scripts/test`, `scripts/lib/`, `.github/`) → an ask for a hand `/go`; `dependabot` (author `dependabot[bot]`, every commit verified, files within `pyproject.toml`/`uv.lock`, not a fork) → `dep_check` → [major bump: ask] → land `--checked` → review; `factory` (a known builder, `(td<id>)` title) → land → review; `external` → read-only analysis → ask → land → review. Params: optional `dry_run`, `repo`, `max_new` (default 10). Armed by one recurring todo — ask a human operator (recipe: `docs/runbooks/dark-factory-arming.md`). Needs no host capability |
| `dep_check`         | `ssh_node`      | A Dependabot PR's `check` node: fetches the PR head the intake classified (by sha, falling back to the pull ref, then verified with `rev-parse`) into `slice/pr-<n>`, relocks when `uv.lock` is stale (committed on the branch) and resolves the lock for python 3.12 and 3.13 with `uv --no-build` — the dependency is never built, imported or run; green pins the plan's land to that worktree and host. Params: `todo_id`, optional `pr`, `target_node`. Refused while the factory kill switch is on. Needs host capabilities `repo_checkout`, `git` (and `uv` on PATH) |
| `pr_action`         | `ssh_node`      | A `pr-intake` plan's `act` node, after its decision ask is answered (refused before): a land answer lets the land run, `land after fixes: <list>` mints a fix slice + review, `request changes` / `close` / `ignore this major` post to the PR with `gh`, `hold` posts nothing; every non-land answer skips the rest of the plan. Params: `todo_id`, optional `target_node`. Refused while the factory kill switch is on. Needs host capabilities `repo_checkout`, `git` (and `gh auth`) |

## Which job types run compute or code? — [[precis-job-observe-help]]

The compute-lane job types (`struct_relax`, `pourbaix_bulk`,
`surface_coverage_scan`, `sandbox_run`, the pathway seed/aggregate
fan-out, pcb/retrosynth lanes) — executors, params, the
`surface_anchors` quest flag and the named-list scan recipe — live in
[[precis-job-observe-help]].

## Submit a job
## Enqueue an offline run
## Kick off an agent task

**Recommended: write the intent as a todo; the dispatch
worker mints the job.** (The `fix_gripe` examples below are
shape illustrations; that lane is OFF, so submit it only by hand.)

```python
# 1) Write the intent under whichever strategic it belongs to.
put(kind='todo',
    text='Fix gripe:42 (rate-limit edge case)',
    parent_id=engineering_hygiene_strategic_id,
    meta={'executor': 'claude_inproc',
          'job_type':  'fix_gripe',
          'params':    {}},
    # Dispatch auto-injects this if you omit it, but explicit is
    # tidier: when the child job succeeds, the parent flips done.
    tags=['STATUS:open'])

# 2) Add the link to whatever the job operates on (for fix_gripe,
#    the gripe).
link(kind='todo', id=<that_todo_id>, target='gripe:42', rel='fixes')

# 3) Walk away. The dispatch worker (in the default rotation) mints
#    the job under it on its next pass — minutes, not seconds (one SYS
#    cycle, 15-18 min observed; prio is ascending, lower = hotter, see
#    precis-minter-help). Poll the parent todo's
#    status if you want; the job lives under it.
```

**Ad-hoc (direct submit) — when you want to skip the intent layer:**

```python
put(kind='job',
    parent_id=<some_todo_id>,        # required — no orphan jobs
    job_type='fix_gripe',
    link='gripe:42', rel='fixes')
# → created job id=101
```

## Queue behind interactive work — prio= / origin=

Claim order is **class, then priority, then time queued** — the same
`(class_rank, aged_prio, queued_since)` key the `queue:` lines on a
queued job report. Two mint-time knobs:

```python
put(kind='job', parent_id=<todo>, job_type='survey_extract',
    origin='background',   # meta.origin scheduling class:
                           # human > agent > recurring > background
    prio=8)                # refs.prio, 1..10 — lower = hotter (NULL reads 5)
```

* `origin=` overrides the context-derived class. An agent's put derives
  `agent` by default — batch work it fans out sits *in the interactive
  band*, ahead of every real background job. Pass `origin='background'`
  so interactive work always claims first. `origin='human'` is refused
  (authenticated web writes only), and a job parented on a coordinator
  job inherits the parent's class — a conflicting `origin=` refuses.
* `prio=` writes the `refs.prio` column (the 0014 scale: 1 preempts
  across *all* classes, 2 is the cron band, 5 the default, 8 commodity
  background). Inside a class, an aged job gains one prio band per
  ~10 min queued, so a high-number prio still can't starve — it just
  claims after fresher work first.
* Reprioritise a live job with `tag(kind='job', id=N, prio=M)` — works
  on any non-terminal job (queued, running, `waiting_*`; the write
  lands on the row's next claim/re-queue). A terminal job refuses:
  nothing claims it again. `prio=`/`origin=` on `mode='retry'` /
  `mode='retarget'` / `mode='setting'` / `mode='wake'` /
  `mode='extend'` are refused — those verbs don't mint.

The handler validates `job_type`, `executor`, `params`, AND the
parent todo's existence at submit time. If something's wrong, the
`put` call fails immediately rather than queueing an unrunnable job.

Executors: `claude_inproc` (offline `claude -p` on the agent host —
the default, you usually don't set `executor=`), `ssh_node` (remote
GPU-node compute, e.g. `struct_relax`), `job_inproc` (deterministic
in-proc work orders — `embed_batch`, `derived_drain`, `pcb_*`,
`datasheet_pull`, `paper_pull`), and `coordinator`
(yield/resume phase machines that fan out child jobs, e.g.
`good_search`). Each executor has a drain pass in the worker rotation
named after it — `job_claude_inproc`, `job_ssh_node`, `job_inproc`,
`job_coordinator` (one coordinator slice per pass) — plus `minter`
(mints `kind='job'` children under executor-bearing todos), `sweeper`
(fails claim-orphaned `STATUS:running` jobs), `diagnose_scan` (mints
`diagnose_gripe` jobs), and `materialize` (mints `embed_batch` /
`derived_drain` work orders above the backlog bands). Those service
names are what `--only`, `service_config` rows and `worker_logs`
handler fields show.

## Sweeper guards — unschedulable, off-lane pins, control starvation

The `sweeper` also guards executability: a `STATUS:queued` job whose
capability requirements no live host advertises in `resource_slots`
(and no `params.target_node` pin falls back on) can never be placed —
it raises `scheduler/unschedulable:<job ref>` at warn, auto-resolved
when the job leaves the queue or the capability is advertised again.
A job pinned by `params.target_node` to a host that still heartbeats
but no longer runs the executor's drain pass (it left the lane: ran
`job_<executor>` this week, silent past 4h, nothing leased there) fails
as infra, tagged `reaped:off-lane-pin`, with `meta.error` naming the
node; `sweeper/off-lane-pin:<node>/<executor>` opens at warn and
resolves when the pass runs on that node again. The sweeper also
watches for lane starvation: a control-class row — origin
`human`/`agent`/`recurring`, a release-band job_type, or unstamped
prio ≤ 2 — claimable on `job_inproc` for over
`PRECIS_CONTROL_STARVED_MINUTES` (default 15m) opens
`jobstarve/control:<executor>` at warn, auto-resolved when nothing
control-class waits past the bound (gr592211: a ~50-deep
`derived_drain` backlog occupied every in-proc slot, so `release_poll`
and `factory_rescue_tick` queued a full drain per turnover). The fix is
the per-host `claim.max_running.<job_type>` cap — `derived_drain`
defaults to 2 running per host — so the alert means the cap isn't
holding (check `get(kind='job', id='/claim')` and the host's claimants)
or the lane is otherwise wedged.

Parenting is polymorphic (ADR 0044): a job parents on a **todo**
(intent lane — rotation, `child-failed` bubble, `child_job_succeeded`),
on a **build subject** (`structure`/`cad`/`draft` — derived compute,
no todo needed), or on a **coordinator job** (campaign children,
spawned by the coordinator itself — never submit these by hand).

## Submit a job tied to a specific parent

The `link` + `rel` pair anchors the job. For `fix_gripe`:
`link='gripe:42', rel='fixes'`. Other job_types use their own
relations.

## Idempotent submit
## Re-submit safely

`idem_key` defaults to the link target (e.g. `gripe:42` for a
fix_gripe job), so a duplicate submit returns the same job id
while an earlier attempt is still queued or running. Once the
prior attempt is terminal (`STATUS:succeeded` / `failed` /
`cancelled`), a fresh attempt is created.

There is no auto-retry — failures stay failed until you ask for
another attempt.

## What jobs are running right now?
## Show me the active queue

```python
search(kind="job", tags=["STATUS:running"])
```

## How long did a job run, and where?

Every terminal `STATUS:` (succeeded, failed, cancelled, timeout kill) stamps `meta.finished_at` (UTC), `meta.wall_s` (finished minus first claim; 0 if it never ran) and `meta.node` (the pinned `target_node`, else the claiming host). `get(kind="job", id=N)` prints them under `status:` alongside `updated_at` (the row's own timestamp — compare it to your clock when a card looks stale). A seed's compute dir is kept until that stamp lands.

## Show me everything queued up

```python
search(kind="job", tags=["STATUS:queued"])
```

`get(kind="job", id=N)` on a queued job adds `queue:` lines: its origin
class, its position in the executor's claim order (class, then priority,
then time queued) with the per-class count of rows ahead, what is running
on that executor now, and holds it can see (operator suspension, a
`target_node` pin, a live lease).

The card's status is verified against a fresh read before queue text is
rendered: when the row's real state moved on (claimed, parked, finished),
the card prints the fresh status or a `stale-read:` line instead of a
position — a terminal job never renders as queued (gr592853).

## Show me failed jobs
## Find jobs that need attention

```python
search(kind="job", tags=["STATUS:failed"])
```

## Show me a specific job
## How did this job go?

```python
get(kind="job", id=101)
# → header + current status + summary chunk (when finished) +
#   recent job_event chunks (telemetry, kept for forensics)
get(id="jo101")  # the job's universal handle resolves the same way
```

A `STATUS:failed` card leads with `error:` — the one-line failure
reason (`meta.error`, else the newest `job_event` head; `retry_after:`
beside it when a transient backoff was stamped) — printed right under
`status:` so the card cap can't cut it on jobs with long event
timelines. The full reason stays in the `job_event` chunks below.

The `job_summary` chunk is the human-readable account ("Fix
landed on main at <remote> as abc123 (squash of gripe_42 onto
def456), confirmed by ls-remote. 3 files changed, 47 insertions(+),
12 deletions(-). Took 84s."), searchable via `search(kind='job', q=...)`.
`job_event` chunks (lease renewals, llm_output excerpts,
commit_made markers) are forensic; default search excludes them.
A `job_result` chunk (a job's structured deliverable — a ranking or
proposal JSON) shows as a capped head on the card;
`get(kind='job', id=<job>, view='result')` returns it in full.
`view='transcript'` is the compact forensic read (one line per tool
call + the final report); a paged full-transcript view is not
implemented — `view='raw'` is the only uncapped transcript read today.
A guessed spelling with one obvious reading is accepted with an
`interpreted:` line rather than refused (gr635827): `view='compute'`
is the card itself — its compute lines (`footprint:`, `node:`,
`wall_s:`, `executor:`, `wall_seconds:`) are the read callers wanted.

## A finished topo pcb job's run files — view='run-files'

A finished `placer='topo'` `pcb_place` job keeps its run artifacts
(`summary.txt`, `manifest.json`, `elites.json`, `pairs.json`,
`pins.json`, `placement.svg`) and the route-stage meta on its own
`refs.meta.topo_run`. Read them through the view — never by digging
`refs.meta` paths by hand (gr524274):

```python
get(kind='job', id=<job>, view='run-files')
# → file names + sizes, the summary text, the route meta
get(kind='job', id=<job>, view='run-files', args={'file': 'manifest.json'})
# → one file's content (JSON sorted, text/SVG verbatim, capped with a note)
```

An unknown `file=` refuses listing the available names; a job with
no `topo_run` gets a clear empty answer. For the board-side drill-down
(run pick, diffs) see `precis-pcb-topo-help` (`view='topo'`).

## A coverage scan's per-point geometry — view='geometry'

A `surface_coverage_scan` job on an engine with `keep_geometry`
(autocatpath >= 0.45) stores every scan point's final relaxed structure,
and for a failed point (unconverged, detached, placement clash) its relax
trajectory subsampled to at most 50 frames, as `scan_geometry` chunks
with the extxyz in a blob (gr540872). They are never embedded, never
summarized, and never shown on the default job card; read them here:

```python
get(kind='job', id=<job>, view='geometry')
# → the points: id, adsorbate, n, FAILED, stored parts + sizes
get(kind='job', id=<job>, view='geometry', args={'point': '(111):O:4'})
# → that point's final relaxed structure (extxyz)
get(kind='job', id=<job>, view='geometry',
    args={'point': '(111):O:4', 'frames': True})
# → a failed point's relax trajectory (multi-frame extxyz)
```

Append-only: a re-run of the job adds new chunks and the newest record of
a point wins; an unknown point refuses listing the stored ids.

## What jobs have run on this gripe?
## History of fix attempts for a gripe

```python
search(kind="job", link="gripe:42")  # most recent first
```

## What jobs have run on this paper / ref / parent?

Same shape: `search(kind='job', link='<kind>:<id>')`.

## Which jobs belong to this quest / todo / board / structure?

`under=` scopes by the parent_id tree AND subject links — jobs under a
quest's todo tree, a coordinator's children, a board's build jobs.
`view='tree'` renders the hierarchy, `depth=`/`status=`/`since=` narrow:

```python
search(kind="job", under="qu459585")                # jobs beneath it
search(kind="job", under="qu459585", view="tree")   # the skeleton
search(kind="job", under="qu459585", view="count",
       args={"group_by": "meta.job_type,state"})
search(kind="job", under="qu459585", status="failed", depth=3)
```

The answer for "is the queue healthy *here*?" — never raw `refs`/`links`
SQL. `link=` stays right for jobs pointing AT a ref; `under=` is the
hierarchy question.

## Jobs of one type — how many ran, finished, failed?

`args={'job_type': ...}` filters on `meta.job_type` — listing, count, and
`under=` all honour it, and `view='count'` + `group_by='state'` gives the
status facets ("minted/finished/failed since <t>"):

```python
search(kind="job", args={"job_type": "graph_garden"})                 # list, recency
search(kind="job", args={"since": "24"})                              # minted last 24h, any type
search(kind="job", args={"job_type": "graph_garden", "since": "48h"}) # one type, age spelling
search(kind="job", view="count",
       args={"job_type": "graph_garden", "group_by": "state"})        # counts by status
search(kind="job", view="count", status="*",
       args={"job_type": "graph_garden", "since": "2026-10-20",
             "group_by": "state"})                                    # in a window
search(kind="job", under="qu459585", args={"job_type": "graph_garden"})
```

`status=` narrows to one STATUS; `since=`/`until=` bound `created_at`
(mint time) — an ISO timestamp or an age (`'24'`/`'6h'`/`'7d'` =
hours/days ago). A window with no `job_type=` and no view lists jobs
of every type minted in it — "what ran in the last day". `q=` is
title-lexical on this path; `sort=`/`mode=` are rejected — they belong
to the unfiltered ranked path. This is the MCP answer for what used
to need `refs.meta->>'job_type'` SQL.

`since=`/`until=` ride `args=` like `job_type=` on the queryless plain
listing too — `search(kind='job', args={'since': '2026-10-20'},
status='*')` is the same recency-ordered listing without a `job_type=`
filter (adding `q=` makes it a ranked search and `sort=`/`mode=` legal
again).

`view='count'` facet rows also name each group's newest/oldest member
(`jo<N>` + created bounds) — "when did the last <type> run, how far
back does the streak reach". The bare `group_by='job_type'` spelling
reads as `meta.job_type`, so
`args={'group_by': 'job_type,state'}` is the one-call diagnosis table
(counts by type × status, per-group span, error-heads trailer for
the failed rows — the fastest lane-health read). Facet rows page
like a listing: `page_size` groups per
page (default 10, raise it for the whole table — `page_size=100`
covers ~39 job types), `page` the window; rows past the page fold
into one `other: N groups, M jobs` tail naming the hidden groups,
and its `next:` is the next page's call.

For the *trend* behind the snapshot — minted vs terminal per day,
with mint→terminal percentiles per lane —
`search(kind='job', view='flow', args={'job_type': '<type>'})`
(`precis-search-help`'s flow section).

## When does a cadence fire next? — [[precis-job-observe-help]]

`get(kind='job', id='/cadence…')` lives in
[[precis-job-observe-help]].

## Cancel a running job
## Stop a job that's taking too long

```python
tag(kind="job", id=101, add=["STATUS:cancel_requested"])
# worker SIGTERMs at the next safe point; final tag is STATUS:cancelled
# a still-queued (never claimed) job goes straight to STATUS:cancelled
# a parked job (STATUS:waiting_*) is cancelled where it sits by the wake runner
```

A detached `ssh_node` job whose process handle cannot be verified on its
node (missing launch identity — e.g. a handle minted before identity
stamping, gr572654) cannot be signalled: the row holds
`cancel_requested` while the plugin's poll follows the child to its real
outcome — a running `land_slice` ship still lands and notifies. Past
`meta.deadline` the row keeps following: the bound applies to the
compute, never to the record — a `cancelled` stamp while the ship ran on
is what dropped the land's post-steps (gr572654). Only a child proven
gone — its process slot recycled or the host rebooted — cancels after
the poll finds no outcome to record. The remote process is never stopped
by tagging alone.

Agents drive only these `tag` edges: queued|yielded ->
cancel_requested|cancelled;
running/submitted/waiting_* -> cancel_requested|failed; cancel_requested ->
cancelled. Any other STATUS (running, succeeded, queued) is
worker-owned and refused; re-run a failed job with `mode='retry'`.
Under a `plan:<id>` node the tick re-queues a cancelled job free —
`run_slice` and allowlisted read-only ops only (`ci_verdict_poll`); a
cancelled `land_slice`/`release_*`/`pr_action` waits for the rescue
pass. Stop the node: `halt:`/`waiting-for:`, `STATUS:paused`, won't-do.

## Move a queued pinned job to another node

A queued job pinned by `params.target_node` waits for that node even when
another is idle. Move the pin (queued jobs only):

```python
put(kind='job', id=<queued_job_id>, mode='retarget',
    params={'target_node': '<host>'}, text='<why>')
```

It refuses a job that is not `STATUS:queued` or not pinned, a node whose
executor lane is retired, and a node that doesn't advertise every resource
the job requires. `text=` is required; the old and new node, the reason and
the actor land in a `job-retarget` event on the job.

## Set or clear a claim.* operator override

The claim substrate reads `claim.*` `app_state` keys live:
`claim.max_running.<job_type>` (per-host running cap, int 0..64 — `0`
suspends the type), `claim.slicing.<executor>` (bool),
`claim.slice_s.<executor>` (positive seconds),
`claim.executor_cap.<executor>` (per-host running cap for the whole
executor — `ssh_node` defaults to 8, absent = uncapped) and
`claim.reserve.<executor>` (JSON `{"<job_type>": <slots>}` of slots
inside the cap held back for those job types — a compute may not take
the last reserved slots; `ssh_node` defaults to
`{"run_slice": 1, "land_slice": 1}`, gr603792). Set or clear them with
the audited setter — `reason=` is required and the change (who, old →
new, why) lands in `worker_logs`:

```python
put(kind='job', mode='setting',
    args={'key': 'claim.max_running.derived_drain', 'value': 4,
          'reason': '<why>'})
put(kind='job', mode='setting',
    args={'key': 'claim.slicing.job_inproc', 'unset': True,
          'reason': '<why>'})
```

The key must name a real job_type / executor and the value the family
type. Writes are CLI-only: only the `cli` source may set or unset.
Web sources, every worker source (`precis-worker*`, `asa-*`), and calls
inside a background pass are denied.
Read the live overrides next to their code defaults with
`get(kind='job', id='/claim')` ([[precis-job-observe-help]]).

## Extend a live job's wall budget and deadline
## Give a running job more time without restarting it

An active job's kill bound is `meta.deadline` (stamped at submit/launch
from `params.resources.wall_seconds` + the executor margin). To give a
live job more time **without resetting elapsed time or restarting the
compute**, extend both stored fields in place — one job, or every live
job under a tree (`under=` resolves the same member+link set as
`search(kind='job', under=…)`):

```python
put(kind='job', id=<job>, mode='extend',
    args={'seconds': 86400, 'reason': '<why>'})
put(kind='job', mode='extend',
    args={'under': 'qu164903', 'job_type': 'autocatpath_seed',
          'seconds': 86400, 'reason': '<why>'})
```

Each call adds `seconds` (1..604800 — a week; a bigger push takes a
second audited call) to the stored `resources.wall_seconds` and to the
existing `meta.deadline` — `old + seconds`, never `now + budget`, so
the deadline stays anchored at its original submit origin. `reason=` is
required; the actor, reason and old→new values land in a `job-extend`
event on each job. A terminal job refuses (`id=`) or is skipped and
named (`under=`); a live job with neither field has nothing to extend.
`under=` touches at most 64 live jobs per call — narrow with
`job_type=` — and refuses outright when the member/job resolution
itself truncates rather than extending a silently incomplete set
(narrow the root or use `job_type=`). The response lists every job's
old→new `wall_seconds` and
`deadline`; both fields also ride the `get(kind='job', id=N)` status
card as `resources.wall_seconds:` / `deadline:`.

## Wake a time-parked job now

`put(kind='job', id=N, mode='wake', args={'reason': '<why>'})` re-queues a
`STATUS:waiting_time` job immediately, clearing its stale lease and recording
the caller and reason in a job event.
It refuses other statuses and mint arguments; use `mode='retry'` for a failed job.
Waits whose `wake_when.payload.why` contains `quota` or `pause` (case-insensitive)
are protected unless you explicitly pass `args={'force': true, 'reason': '<why>'}`.

## Re-submit a failed job
## Retry a failed job
## Re-run a job with a different model

When a job fails, its parent todo gets `child-failed:<job_id>`
tagged. The doable view excludes parents with that tag so they
don't keep getting re-picked. The sweeper auto-unparks bounded
(see above), so manual retry is for *now* rather than *eventually*
— and mandatory only once `child-failed-final` is latched:

```python
# Option A (preferred): the retry verb. One call clears the bubble
# so the dispatch worker re-mints a fresh attempt on its next sweep
# (~1 min). The failed job stays for forensics. If the parent has
# also been terminally parked (child-failed-final latched by the
# sweeper), retry strips that latch too and resets the unpark budget
# in the same call — so this is the one recipe that unparks a
# child-failed-final leaf without knowing the tag names.
put(kind='job', id=<failed_job_id>, mode='retry')

# Change the model at the same time (opus | sonnet | haiku). This
# swaps the parent todo's meta.llm_tier before clearing the bubble,
# so the re-minted tick runs on the new tier. Only valid when the
# parent is an LLM-planner todo (already has meta.llm_tier set) —
# handy when a tick hit an AUP refusal or needs a stronger/cheaper
# model. The web Todo tab exposes the same thing as a "Retry" button
# (with a model dropdown) on failed job rows — turn on "+ show closed
# jobs" to see them.
put(kind='job', id=<failed_job_id>, mode='retry', model='sonnet')

# The parent's meta.llm_select pin (placement/thinking/effort/
# temperature) survives a plain retry — the ack names it when it
# does. A surviving placement='local' pin re-mints the same failure;
# override it in the same call:
put(kind='job', id=<failed_job_id>, mode='retry', placement='cloud')
# select={...} is the full-knob form; placement= is its one-key
# shorthand and wins the placement key when both are given.

# Retry rejects a job that isn't terminal (STATUS:failed/cancelled),
# an orphan job with no todo parent, and model=/select=/placement=
# on a non-LLM parent.

# Option B (manual equivalent): clear the bubble by hand. A failed
# child is terminal, so it does NOT block re-mint — deleting it is
# optional cleanup, not required.
tag(kind='todo', id=<parent_id>, remove=[f'child-failed:{failed_job_id}'])
# If the leaf is terminally parked (has child-failed-final too), you
# MUST remove BOTH tags in ONE call — removing child-failed-final
# alone does not stick: the sweeper re-latches it on its next cycle
# while the child-failed:<job_id> tag is still open and
# unpark_attempts is at the cap. Prefer Option A, which also resets
# the unpark budget; the manual two-tag removal leaves unpark_attempts
# at the cap, so one more failure re-latches child-failed-final
# immediately.
tag(kind='todo', id=<parent_id>,
    remove=['child-failed-final', f'child-failed:{failed_job_id}'])
# (optionally) delete(kind='job', id=<failed_job_id>)
# Dispatch worker mints a fresh job on the next tick.

# Option C: different executor or job_type — edit the parent's meta
# first, then clear the bubble as above.
# (No direct meta-patch verb today; edit it via the runtime, or
# delete the parent todo and re-create with the new shape.)

# Option D: ask the user (asa-bot pattern).
put(kind='todo',
    parent_id=<parent_id>,
    text='Job #N failed with X — should I retry, switch executor, or skip?',
    tags=['ask-user'],
    meta={'auto_check': {
        'type': 'discord_reply_received',
        'ask_message_id': '<discord msg id>'}})
put(kind='message',
    target='discord/<guild>/<channel>/<thread>',
    text='Hey, parent #N needs your call: ...')
```

`idem_key` defaults to the link target so a stray duplicate submit
returns the in-flight job's id rather than queueing twice.

## Why didn't my job run?
## My put(kind='job', ...) was rejected — why?

Rejection reasons surfaced at `put` time, not later:

- **Missing `parent_id`** — every new job must
  declare its parent todo. The error names the canonical
  dispatch-from-todo pattern.
- **Bad `parent_id`** — the integer doesn't address a live
  `kind='todo'` ref.
- Unknown `job_type` — not in the registry.
- Executor doesn't list this `job_type` in its
  `COMPATIBLE_EXECUTORS` set.
- Executor host doesn't provide everything in the type's
  `REQUIRES` set.
- Bad `params` (jsonschema validation failure).
- Params that type-check but can't run — a `job_type` may declare a
  put-time `validate(store, params)` check (e.g. `survey_extract`
  requires `measurands` to be live taxa); it refuses the put instead
  of queueing a job that fails at dispatch.

The error message names the missing piece. Catch it, fix it, re-
submit.

## What does each job_type require to run?

Each `job_type` module declares `PARAMS_SCHEMA`,
`COMPATIBLE_EXECUTORS`, `REQUIRES`, and a `DESCRIPTION`. The
per-type recipe skills (`precis-fix-gripe-help`, …) document the
shapes for the LLM-facing call.

## Status vocabulary

| Tag                       | Meaning                                |
|---------------------------|----------------------------------------|
| `STATUS:queued`           | Filed, waiting for a runner            |
| `STATUS:yielded`          | Sliced job released mid-run; claimable |
| `STATUS:submitted`        | Handed to an external system (cluster) |
| `STATUS:running`          | A runner has claimed it                |
| `STATUS:succeeded`        | Finished cleanly; check `job_summary`  |
| `STATUS:failed`           | Exited without a usable result         |
| `STATUS:timed-out`        | Killed at its wall-clock deadline with the job type's timed-out verdict (`land_slice`); the plan tick parks the slice at the re-land ask |
| `STATUS:cancel_requested` | Cancellation in flight                 |
| `STATUS:cancelled`        | Runner stopped on cancel request       |

(`STATUS:submitted` is used only by future cluster executors;
`claude_inproc` goes straight queued → running. A `coordinator` job
additionally parks at `STATUS:waiting_children` / `waiting_time` /
`waiting_ask_user` / `waiting_manual_kick` between slices — that's a
legitimate pause, not a stall; the `wake_runner` re-queues it when
the wake condition fires.)

## A coordinator job yields a pending question — `STATUS:waiting_ask_user`
## Why is this job parked waiting on me?

A `coordinator` job_type (long-running campaigns like `good_search`)
can pause on a human decision instead of failing or blocking: its
dispatcher tags itself with an open `ask-user:<phase>:<slug>` pattern
and returns `Yield(state=<checkpoint>, wake_when=WakeWhen('tag_cleared',
{'tag': 'ask-user:<phase>:*'}))`. The executor checkpoints that state
into `meta.coordinator_state`, sets `STATUS:waiting_ask_user`, and
releases the runner slot — parked, not stuck. `wake_runner` polls for
the matching tag to disappear and re-queues the job to `STATUS:queued`
so the next slice resumes from the checkpoint.

Answer the pending question by clearing the matching tag on the job
(`tag(kind='job', id=42, remove=['ask-user:<phase>:<slug>'])`) once
you've decided. This is the job-level sibling of the todo-level
`ask-user`/`ask-user:<question>` open tag a dispatched agent tags on
its own todo to yield the same way — see `precis-todo-tree-help`,
`precis-decomposition-help` for that path.

## What is the cluster actually doing right now? — [[precis-job-observe-help]]

Fleet-introspection views `get(kind='job', id='/…')` — `/logs`
(worker_logs, filterable by handler/host/process/job/since/level/q,
also via `args=`), `/limits` (provider pacing), `/delta` (since the
last doctor tick), `/builds` (per-process code + `contains_sha`),
`/throughput` (claims + queue depth), `/settings` (app_settings +
service_config rows, read-only) — live in
[[precis-job-observe-help]].

## See also

- [[precis-job-observe-help]] — compute-lane job types + the `/…` fleet views
- [[precis-gripe-help]] — the bug tracker
- [[precis-fix-gripe-help]] — fix_gripe recipe
- [[precis-search-help]] — find jobs by link/status

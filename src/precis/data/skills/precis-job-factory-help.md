---
id: precis-job-factory-help
family: work
title: precis — dark-factory and PR job types
summary: the factory lane job_types — run_slice, land_slice, plan_dispatch, factory_rescue_tick, ci_watch, gripe_validate, lane_health, boundary_probe, and the pr-intake plan (pr_poll, dep_check, pr_action); the release lanes are precis-job-release-help
answers:
  - how do I dispatch an existing todo to a factory lane right now?
  - which job types run slices, land branches and drive plans?
  - what does the plan_dispatch tick do and how is it armed?
  - which job types watch CI, validate gripes and probe lane credentials?
  - how do inbound pull requests get handled (pr-intake plan)?
applies-to: put (kind='job', args={'job_type': …}); get/search (kind='job')
tags: workflow, verbs
kinds: job, todo
status: active
---

# precis-job-factory-help — dark-factory and PR job types

The `job_type` registry for the dark factory: the lanes that run
slices, the plan ticks that drive todo subtrees, the watchers and
the PR intake — the release pass and release-fix lanes live in
[[precis-job-release-help]]. Submitting a plan (the recipe an
agent normally wants) lives in [[precis-factory-submit-help]]; the
generic submit shape and dispatch recipes in [[precis-job-help]].
Everything below is armed by recurring todos a human operator
mints — the recipes are in `docs/runbooks/dark-factory-arming.md`.

## Dispatch an existing todo to a lane now

The short form: `meta.op` + a self `plan:<id>` tag turns any open
todo into a one-node plan the `plan_dispatch` tick runs — the two
calls are in [[precis-job-help]]. The tick requires `STATUS:open`,
no live `blocked-by` target, no `waiting-for:*` / `halt` /
`claimed-by:` park, no live child job, under its retry cap.
`meta.op` resolves to `{type, params}` (a bare name runs
`{type: name, params: {todo_id}}`, default `run_slice`). Never put
`meta.executor` on a plan node — the tick owns the jobs.

## Dark-factory job types — the lanes that run slices

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `run_slice`         | `ssh_node`      | Launch a slice todo through its enabled lane's adapter (`claude_p`, `codex_exec`, `devin_tree`) in worktree `slice-td<id>`, branch `slice/<id>` from `origin/main` (from `origin/release/r<N>` when the todo's `meta.land_target` or a `land target: release/r<N>` brief line names one). No DB credentials; only Claude gets precis MCP. Codex and Devin record everything in their final message, posted as `handoff:`. Devin refuses explicit models outside the lane's list; unpinned vendor lanes without a default pass no model flag. Job metadata records `lane`/`adapter`/`model`/`effort`. Every branch is checked against `deny_write_paths` (`STRICT_DENY` — FACTORY plus the human-only set — or `HUMAN_ONLY_PATHS + INTERIM_DENY` while the DB-only `factory.deny_relaxed` setting is on); a violation fails. A todo's `repo:<name>` tag (its own, else its nearest tagged ancestor's; none means precis) picks the repo; `plan_dispatch` stamps `params.repo` for a non-default one and submit refuses a mismatch. A non-default repo builds from its `RepoProfile` and only when the DB-only setting `factory.open_repos` lists it (closed by default). Clean exits with commits mint `land_slice`; failures reopen the todo with `attempt:<n>`. Generator roles: `reviewer` posts `review:`, `analyst` posts `analysis:`, others create child todos. Params `todo_id`, optional `lane`, `model`, `effort`, `head_sha`, `target_node`, `repo`. Kill switch recovery: ask an operator (`docs/runbooks/dark-factory-arming.md`). Host capabilities remain `repo_checkout`, `claude_bin`, `git`; missing capabilities leave the job queued with a scheduler alert |
| `land_slice`        | `ssh_node`      | Dark factory lander, minted by `run_slice` (one per slice, idem key `land_slice:td<id>`): `scripts/ship --quick "<the slice's commit subject>"` in the slice worktree, no LLM. A `repo` other than precis is admitted only when `params.repo` names an open repo whose profile lander is `land_slice`; it then squashes the branch, runs the profile's gate and pushes a plain fast-forward, with no release, tag or pull request. A todo with a land target (`meta.land_target = release/r<N>`, a release fix) ships `--quick --onto release/r<N>` instead: the change lands on that release branch only and main does not move. Green → `meta.landed_sha`, a `landed: <sha>` comment + `STATUS:done` on the todo when the ship's land-notify did not write them, the sha written into the plan's pending `sha-in-prod` gate, then the worktree and `slice/<id>` branch (local and origin) deleted. Red (lint ✖, merge conflict) → the job fails with the ✖ line, a `handoff:` comment names it, the todo returns to `STATUS:open` with `attempt:<n>` and the branch stays for the next build. With `pr: <n>` (a `pr-intake` plan's land node, `checked: true`) it lands the PR head on `slice/pr-<n>` with `--checked` and closes the PR with a comment naming the squash sha. Params `todo_id` plus `worktree` + `branch` (a slice) or `pr`; optional `checked` (`true` selects `--checked`; default and `false` are `--quick`; `run_slice` mints it when the worker committed the tree it was left or the last `tests:` marker in its final message is not `ran`), `lane`, `target_node`. Security boundary: `src/precis/plans/boundary.py` registers the path sets and added-line rules (`boundary_hits`) whose diff routes the land through the `security-review` lane — read-only today, nothing calls it yet (td574934 A2). Needs host capabilities `repo_checkout`, `git` (pinned to the host that ran the slice) |

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

## Release lanes

The release pass and release-fix job types (`release_cut`,
`ci_verdict_poll` incl. `restamp`, `release_deploy`,
`release_confirm` + `merge_back`, `ci_red_digest`, `ci_rerun`,
`release_poll`) moved to [[precis-job-release-help]].

## Inbound pull-request job types — the pr-intake plan

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `pr_poll`           | `job_inproc`    | Inbound pull requests: lists the repo's open PRs (`api.github.com`, `GH_TOKEN`, else the factory's vault token) and mints one `pr-intake` plan, tagged `pr:<n>`, per non-draft PR that has none, plus the head it was minted on as `pr-head:<sha12>` (a PR with an open plan, or a closed one on the same head, is skipped; a head that moved after its plan closed gets a new plan). The class is decided in code: `harness` (touches `scripts/ship`, `scripts/test`, `scripts/lib/`, `.github/`) → an ask for a hand `/go`; `dependabot` (author `dependabot[bot]`, every commit verified, files within `pyproject.toml`/`uv.lock`, not a fork) → `dep_check` → [major bump: ask] → land `--checked` → review; `factory` (a known builder, `(td<id>)` title) → land → review; `external` → read-only analysis → ask → land → review. Params: optional `dry_run`, `repo`, `max_new` (default 10). Armed by one recurring todo — ask a human operator (recipe: `docs/runbooks/dark-factory-arming.md`). Needs no host capability |
| `dep_check`         | `ssh_node`      | A Dependabot PR's `check` node: fetches the PR head the intake classified (by sha, falling back to the pull ref, then verified with `rev-parse`) into `slice/pr-<n>`, relocks when `uv.lock` is stale (committed on the branch) and resolves the lock for python 3.12 and 3.13 with `uv --no-build` — the dependency is never built, imported or run; green pins the plan's land to that worktree and host. Params: `todo_id`, optional `pr`, `target_node`. Refused while the factory kill switch is on. Needs host capabilities `repo_checkout`, `git` (and `uv` on PATH) |
| `pr_action`         | `ssh_node`      | A `pr-intake` plan's `act` node, after its decision ask is answered (refused before): a land answer lets the land run, `land after fixes: <list>` mints a fix slice + review, `request changes` / `close` / `ignore this major` post to the PR with `gh`, `hold` posts nothing; every non-land answer skips the rest of the plan. Params: `todo_id`, optional `target_node`. Refused while the factory kill switch is on. Needs host capabilities `repo_checkout`, `git` (and `gh auth`) |

## See also

- [[precis-factory-submit-help]] — mint a plan (build + reviewer + land) by hand or template
- [[precis-job-help]] — submit shape, `args=` channel, the dispatch recipe
- [[precis-job-release-help]] — release pass / release-fix / release_poll lanes
- [[precis-job-types-help]] — the non-factory job_type registry
- [[precis-job-observe-help]] — compute job types + fleet `/…` views
- [[precis-job-ops-help]] — cancel / retry / re-queue rules under a plan
- [[precis-gripe-loop-help]] — the coordinator's loop on gripe fixes

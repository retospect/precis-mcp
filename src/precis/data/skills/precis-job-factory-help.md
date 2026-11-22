---
id: precis-job-factory-help
family: work
title: precis — dark-factory and PR job types
summary: the factory lane job_types — run_slice, land_slice, plan_dispatch, factory_rescue_tick, ci_watch, gripe_validate, lane_health, boundary_probe, and the pr-intake plan (pr_poll, dep_check, pr_action); the release lanes are precis-job-release-help
answers:
  - how do I dispatch an existing todo to a factory lane right now?
  - which job types run slices, land branches and drive plans?
  - what does the plan_dispatch tick do?
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
The ticks and watchers below run only while an operator has armed them,
and are read-only or refused while the factory kill switch is on.

## Dispatch an existing todo to a lane now

The short form: `meta.op` + a self `plan:<id>` tag turns any open
todo into a one-node plan the `plan_dispatch` tick runs — the two
calls are in [[precis-job-help]]. The tick requires `STATUS:open`,
no live `blocked-by` target, no `waiting-for:*` / `halt` /
`claimed-by:` park, no live child job, under its retry cap.
`meta.op` resolves to `{type, params}` (a bare name runs
`{type: name, params: {todo_id}}`, default `run_slice`). Never put
`meta.executor` on a plan node — the tick owns the jobs.

A build also waits while its source gripe has an open `blocked-by`
target: the tick mints no `run_slice` behind it (it records the
`dispatch_hold` on the todo) and a queued build is held at claim. Held
builds are not rescued or counted as a stalled chain.

## Dark-factory job types — the lanes that run slices

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `run_slice`         | `ssh_node`      | Launch a slice todo through its enabled lane's adapter (`claude_p`, `codex_exec`, `devin_tree`) in its own clone `slice-td<id>-j<job>` (branch `slice/<id>`) cut from the build host's hub at the task's newest pushed work, else at `origin/main` (`origin/release/r<N>` when the todo's `meta.land_target` or a `land target: release/r<N>` brief line names one). Work goes to hub ref `refs/factory/build/<id>/<job>`, never GitHub; the worker saves leftovers there at job end. The next build, review and land run on the host `meta.build_ref` names. Only Claude gets precis MCP. Codex and Devin record everything in their final message, posted as `handoff:`. Devin refuses explicit models outside the lane's list; unpinned vendor lanes without a default pass no model flag. Job metadata records `lane`/`adapter`/`model`/`effort`. A branch that touches a human-only path fails. A todo's `repo:<name>` tag (its own, else its nearest tagged ancestor's; none means precis) picks the repo; `plan_dispatch` stamps `params.repo` for a non-default one and submit refuses a mismatch. A non-default repo builds only when it is open for the factory (closed by default). Clean exits with commits mint `land_slice`; failures reopen the todo with `attempt:<n>`. Generator roles: `reviewer` posts `review:`, `analyst` posts `analysis:`, others create child todos. Params `todo_id`, optional `lane`, `model`, `effort`, `head_sha`, `target_node`, `repo`. A job no host can place stays queued |
| `land_slice`        | `ssh_node`      | Dark factory lander, minted by `run_slice` (one per slice, idem key `land_slice:td<id>`): ships `params.head` (hub ref `params.ref`) with the quick gate in its own clone, no LLM. A `repo` other than precis is admitted only when `params.repo` names an open repo whose profile uses this lander; it then squashes the branch, runs the repo's gate and pushes a plain fast-forward, with no release, tag or pull request. A todo with a land target (`meta.land_target = release/r<N>`, a release fix) ships onto `release/r<N>` instead: the change lands on that release branch only and main does not move. Green → `meta.landed_sha`, a `landed: <sha>` comment + `STATUS:done` on the todo when the ship's land-notify did not write them, the sha written into the plan's pending `sha-in-prod` gate, then the clone dropped, the task's hub refs deleted and `meta.build_ref` cleared. Red (lint ✖, merge conflict) → the job fails with the ✖ line, a `handoff:` comment names it, the todo returns to `STATUS:open` with `attempt:<n>` and the work stays in the hub for the next build (a rebase conflict stamps `meta.conflict`: a conflict-only attempt follows). With `pr: <n>` (a `pr-intake` plan's land node, `checked: true`) it lands the PR head on `slice/pr-<n>` with the checked gate and closes the PR with a comment naming the squash sha. Params `todo_id` plus `branch`, `ref` + `head` (a slice; `worktree` is ignored) or `pr`; optional `checked` (`true` selects the checked gate; default and `false` the quick one; `run_slice` mints it when the worker committed the tree it was left or the last `tests:` marker in its final message is not `ran`), `lane`, `target_node`, `repo`. |

On an MCP-enabled `run_slice` lane, a healthy first launch event reads
"(mcp token: slice …)". "(mcp token: shared)" means the builder has no
precis MCP grant: every MCP call fails, it cannot read comments or skills,
and it cannot be re-briefed by comment. The brief is still rendered at
launch, so it must be self-contained; put corrections in a `brief:`
comment before the job is minted or retried. Codex and Devin intentionally
run without precis MCP. Host-side token-dir recovery belongs to
`docs/runbooks/dark-factory-arming.md`.

## Dark-factory job types — second-repo release and pin

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `repo_release`      | `ssh_node`      | Mechanical release of a second repo, no LLM: holds the repo's land lease (lands queue behind it; a holder 15 min past its deadline raises the `repo_lease` `stale-holder` alert and is ignored), bumps the version in `pyproject.toml` and `uv.lock`, pushes the commit, waits for the CI run on that sha (a cancelled run is re-run at most twice), then creates the `v<version>` tag and writes `released: <tag> <sha> (CI run <id>)` with `meta.repo_release` (`state: tagged`). Red CI or a taken tag fails the job. Submit is refused unless the repo is open, its profile names a `releaser` (catpath does) and the release node carries `meta.retries = 1` (a blind retry spends a version), and (`holding:`) while an open slice or reviewer of the repo remains under the plan root. Params `todo_id`, `repo`, optional `bump` (`auto`, `minor`, `patch`). |
| `pin_bump`          | `ssh_node`      | Moves precis's pin of a released second-repo engine, no LLM: edits `pyproject.toml`, `uv.lock`, `deploy/group_vars/all.yml` and the coverage anchor, commits in its own tree, puts the commit in the hub as its attempt ref, mints its `land_slice` and succeeds. The node stays open after the land (and for a no-op) until the `precis-dev` gate image on the land host carries the pin (its `precis.autocatpath_rev` label is the pinned sha, the sha a later pin on precis main locks, or a descendant); the next `pin_bump` job waits for that, re-checking every 2 min, then closes the node, so glue blocked by it waits too. The factory never rebuilds the image: a stale, unlabelled or missing image (or docker unreachable for 6 h) files one ask naming `scripts/build-image precis-dev` on that host, filed again at most hourly if it is closed while the image still lacks the pin; a wait behind for over 6 h also raises the `stale-open` alert. That land admits the human-only `deploy/group_vars/all.yml` only when the branch is the node's one commit and the file changes just its `autocatpath_min_version` line. One at a time (`holding:`). An open node 6 h past its release tag with no live job raises the `pin_bump` `stale-open` alert. Needs a `pinner` profile (catpath). Params `todo_id`, `release_todo`, `pin_repo`, optional `target_node`. |

## Dark-factory plan passes — plan_dispatch and factory_rescue_tick

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `plan_dispatch`     | `job_inproc`    | Dark factory: one pass over every plan subtree (`plan:<id>` tags, gate/loop/generate/auto_close nodes) — evaluate gates, re-mint loop bodies, close finished phases, mint one job per doable task (`meta.op`, default `run_slice`) in (prio ascending, id) order across every plan root — a node without a prio takes its nearest ancestor's, default 5 — act on a reviewer node's `review: LAND` / `review: FIX — <list>` / `review: REVERT — <why>` comment (close it; mint a fix task; mint a revert task and reopen the build), release claims older than 4 h, and park a task at its retry cap — `meta.retries` wins, else the lane-ladder length (2 for a model-pinned or ladder-less op) — into rescue; an `ask-user` + `showstopper` todo only on a human-only hit, no enabled lane, or a rescue's own ask. Params: optional `dry_run`. Read-only while the factory kill switch is on |
| `factory_rescue_tick` | `job_inproc`  | Dark-factory stall watchdog: one DB-only pass judges every dispatchable plan node and `sha-in-prod` gate against its stage deadline (idle 20 min, queued 30 min, build 5 h, land 6 h, review 45 min, gate 72 h) — a node silent past it goes to RESCUE (one `rescue:<class>` tag, one gripe per failure signature): cancelled newest job, never dispatched, `post-land-write` (pending-sha gate after the build landed), `terminal-root` (a signal-dead non-terminal job left under the closed root is cleared `STATUS:cancelled`, linked onto the same signature gripe, not rescued), lane no-fit. Holds (`waiting-for:`, `halt`, paused, open ask, `rescue:`, live `blocked-by`), parked nodes (fit lanes full/backed off) and `release-pass` plans are exempt. Then it runs the diagnose pass: a node in rescue gets one `claude-sub` diagnose task — `rescue: REARM` re-arms it once with a corrected brief, anything else files one `ask-user` todo blocking it — except a node under a closed ancestor (`terminal-root`, or a `won't-do` up its parent chain), which closes `STATUS:won't-do` with one note and no ask. Params: optional `dry_run`. Read-only while the factory kill switch is on |

## Dark-factory gripe passes — ci_watch and gripe_validate

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `ci_watch`          | `job_inproc`    | CI watcher: each completed check.yml run is judged once — a red `event=schedule` nightly on main or a red `release/**` run carrying a `soak` job files one gripe per failure signature (`ci:check.yml:<file>::<test>` parametrise-stripped, `ci:check.yml:infra:<job>` tagged `fix:infra` for a node-less failed job); filing bodies and `seen again:` comments on matching gripes deduplicate replay by complete run ID, including after closure. A new run adds a `seen again:` comment to an open match or refiles (under 7 days it names the closed handle), unless its head predates a fix a matching gripe names (`resolved: <sha>` or `moot: … already fixed by <sha>`). An open `ci:mute` tag silences it, and an unresolved gripe with no live todo link is flake-closed after two consecutive green runs while a `resolved:` sha gets `green on <url>` once a green head contains it. Params: optional `dry_run`, `repo`, `lookback_runs`. Read-only while the factory kill switch is on |
| `gripe_validate`    | `job_inproc`    | Gripe ladder, last rung: close every `STATUS:verify` gripe whose `resolved: <sha>` comment names a sha in the recorded prod head (head or landed) as `STATUS:done`; one with no sha, or a sha not yet deployed, stays. Params: optional `dry_run` |

## Lane health — the hourly credential probe (gr588221)

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `lane_health`       | `ssh_node`      | Lane credential probe, minted hourly while any lane is enabled: one cheap auth check per enabled lane on a launchable adapter. A red (`auth`/`outage`) probe parks the lane out of dispatch — skipped exactly like `enabled=False`; a green probe on a parked lane clears the park and resolves its `lane-parked:<lane>` alert; a lane still parked past 6 h files the deduped alert. `enabled` stays the human switch — nothing here writes it |

## Boundary probes — the mechanical branch fence (td574934 A4)

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `boundary_probe`    | `ssh_node`      | Mechanical fence for a slice branch (no LLM, no store writes): for each boundary the branch diff hits (`envelope`, `slice_env`, `safe_fetch`, `deny_list`), it runs that boundary's probe against the branch tree in an isolated test container with no network, plus the branch's version of the probe when the diff changes or adds it (both must pass; a deleted probe is cannot-run). A probe exits pass, breach with the finding, or cannot-run (including a timeout). `meta.probes` records each run; a boundary with no registered probe is refused `unprobed`; anything but all-pass fails the job. Params: `worktree`, optional `branch`, `target_node` |

## Release lanes

The release pass and release-fix job types (`release_cut`,
`ci_verdict_poll` incl. `restamp`, `release_deploy`,
`release_confirm` + `merge_back`, `ci_red_digest`, `ci_rerun`,
`release_poll`) moved to [[precis-job-release-help]].

## Inbound pull-request job types — the pr-intake plan

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `pr_poll`           | `job_inproc`    | Inbound pull requests: lists the repo's open PRs and mints one `pr-intake` plan, tagged `pr:<n>`, per non-draft PR that has none, plus the head it was minted on as `pr-head:<sha12>` (a PR with an open plan, or a closed one on the same head, is skipped; a head that moved after its plan closed gets a new plan). The class is decided in code: `harness` (touches the ship, test or CI machinery) → an ask for a hand `/go`; `dependabot` (author `dependabot[bot]`, every commit verified, files within `pyproject.toml`/`uv.lock`, not a fork) → `dep_check` → [major bump: ask] → land `--checked` → review; `factory` (a known builder, `(td<id>)` title) → land → review; `external` → read-only analysis → ask → land → review. Params: optional `dry_run`, `repo`, `max_new` (default 10) |
| `dep_check`         | `ssh_node`      | A Dependabot PR's `check` node: fetches the PR head the intake classified (by sha, falling back to the pull ref, then verified with `rev-parse`) into `slice/pr-<n>`, relocks when `uv.lock` is stale (committed on the branch) and resolves the lock for python 3.12 and 3.13 without building — the dependency is never built, imported or run; green pins the plan's land to that worktree and host. Params: `todo_id`, optional `pr`, `target_node`. Refused while the factory kill switch is on |
| `pr_action`         | `ssh_node`      | A `pr-intake` plan's `act` node, after its decision ask is answered (refused before): a land answer lets the land run, `land after fixes: <list>` mints a fix slice + review, `request changes` / `close` / `ignore this major` post to the PR with `gh`, `hold` posts nothing; every non-land answer skips the rest of the plan. Params: `todo_id`, optional `target_node`. Refused while the factory kill switch is on |

## See also

- [[precis-factory-submit-help]] — mint a plan (build + reviewer + land) by hand or template
- [[precis-job-help]] — submit shape, `args=` channel, the dispatch recipe
- [[precis-job-release-help]] — release pass / release-fix / release_poll lanes
- [[precis-job-types-help]] — the non-factory job_type registry
- [[precis-job-observe-help]] — compute job types + fleet `/…` views
- [[precis-job-ops-help]] — cancel / retry / re-queue rules under a plan
- [[precis-gripe-loop-help]] — the coordinator's loop on gripe fixes

---
id: precis-job-release-help
family: work
title: precis — dark release-pass and release-fix job types
summary: the release lane job_types — release_cut, ci_verdict_poll (incl. restamp), release_deploy, release_confirm + merge_back, the release-fix loop (ci_red_digest, ci_rerun) and the release_poll starter + stuck watchdog
answers:
  - which job types run a dark release pass (cut / poll / deploy / confirm)?
  - what does the release-fix loop do on a red CI run?
  - how does a release pass get started and what watches a stuck one?
applies-to: put (kind='job', args={'job_type': …}); get/search (kind='job')
tags: workflow, verbs
kinds: job, todo
status: active
---

# precis-job-release-help — dark release-pass and release-fix job types

The `job_type` registry for the dark release lanes: the `release-pass`
plan's cut/poll/deploy/confirm nodes, the `merge_back` mode, the
release-fix loop's digest and rerun, and the `release_poll` starter
that mints a pass only when its gates pass. Slice/land and plan ticks
live in [[precis-job-factory-help]]; the generic submit shape and the
dispatch recipe in [[precis-job-help]]. Everything below is armed by
recurring todos a human operator mints — the recipes are in
`docs/runbooks/dark-factory-arming.md`.

## Dark release-pass job types — the cut/poll/deploy lanes

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `release_cut`       | `ssh_node`      | Dark release pass, the `cut` node of a `release-pass` plan: `scripts/release --cut-only` in the repo checkout cuts `release/r<N>` from main and returns without waiting for CI (first superseding a stale local `round.json` — `release.round` says `confirmed`); the job stamps `meta.release = {rN, sha, range, repo}` on the plan root and the real range on the plan's `tests-green` gate and loop. No `RELEASE_CUT` line (already live, a release in flight elsewhere) fails the job. Params: optional `todo_id`, `target_node`. Refused while the factory kill switch is on, or while a reviewer on an engine-stamped `land_slice` job is open or its FIX is unlanded (the engine-review gate; `release_poll` runs the same check before minting). Needs host capabilities `repo_checkout`, `git` |
| `ci_verdict_poll`   | `job_inproc`    | Dark release pass, the `poll` node: reads check.yml's GitHub Actions run on the cut's sha (`api.github.com`, `GH_TOKEN`, else the factory's vault token) and records green/red in `release.ci_verdicts`, which the `tests-green` gate reads; while CI runs the job parks itself (`STATUS:waiting_time`) and re-polls every 10 min. With `restamp: true` (the release-fix loop's `restamp` node, after the fix nodes) it reads no verdict: it moves `meta.release` onto `release/r<N>`'s current head (sha, range end, the gate and the loop's `until`; no cut), so the next poll and digest key on the fixed head. Params: `todo_id` (reads the plan root's `meta.release`) or `sha` + optional `range`; optional `repo` (else `release.repo`, else the checkout's origin), `restamp`. Needs no host capability |
| `release_deploy`    | `ssh_node`      | Dark release pass, the `deploy` node: `scripts/release --deploy-only r<N>` deploys the plan's cut pinned and records `release.prod_head`. Refused unless the approval ask blocking the node is answered `deploy r<N>` for that cut — Reto's answer, or the tick's auto-answer under `release.auto_deploy` (exact-sha green CI + an open `release.round` on that head). Params: `todo_id`, optional `target_node`. Refused while the factory kill switch is on. Needs host capabilities `repo_checkout`, `git` (pinned to the release owner's host) |

## Dark release pass — the confirm and merge-back lane

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `release_confirm`   | `ssh_node`      | Dark release pass, the `confirm` node after `deploy` (`retries: 0`): attests the deployed runtime — every live worker's heartbeat `code_sha`, read from the store with no network, plus the web's `X-Precis-Boot-Sha` header when the `release.health_url` operator setting names its `/healthz` (unset means the web is not attested; the workers decide). On pass it runs `scripts/round deploy --confirm-runtime <sha> --runtime-evidence "<text>"` then `scripts/round close` and stamps the evidence on the plan root and `release.round`; a failed close still succeeds (the release is already `deployed/r<N>`). Any evidence mismatch or gap fails the job before any script runs and the tick's retry-cap ask goes to a human — no rollback, no `good/r<N>` tag. With `merge_back: true` (the pass's `merge_back` node, also after `deploy`) it instead merges `origin/release/r<N>` into main as `Merge release/r<N> fix forward` and pushes main with the factory credential: no commit past the cut is a no-op; a real conflict pushes nothing and mints one resolver slice (lands on main normally) plus a second `merge_back` node behind it with `strategy: "ours"` and `resolver: <todo id>`, whose `git merge -s ours` records the release head as an ancestor of main once the resolver landed; the pass root stays open until both close. A `release/r<N>` already retired on origin (round close deletes it once its head is on main — the queued merge_back can run behind the delete, gr660285) is the same no-op when the recorded head is already on main, merges on the recorded sha while it is still fetchable, and only fails when no usable head remains. Params: `todo_id`, optional `target_node`, `merge_back`, `strategy`, `resolver`. Refused while the factory kill switch is on. Needs host capabilities `repo_checkout`, `git` (pinned to the release owner's host) |

## Dark release-fix loop — the digest and rerun lanes

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `ci_red_digest`     | `ssh_node`      | Dark release pass, the `digest` node of the `release-fix` loop body: `scripts/ci-failed <run_id>` digests the newest red check.yml run on the cut's sha and posts it — secret-masked, capped — as one `brief:` comment on each `run_slice` todo `blocked-by` the digest node (the fixer's brief). A digest whose failing ids parse complete is split by the failing `src` module its assertion or import line names, else by test file (at most 8 clusters): one fix node per cluster, the extra ones minted beside the iteration's own, each landing on `release/r<N>`. A test red of 1-2 ids that passes the hard-stop lists may instead be waived (`release.red_waiver`, default `on`): a `broken-build` gripe, the verdict recorded green-waived, no fix session. With `params.probe` + `params.run_id` instead runs `scripts/ci-failed <run_id> --probe` and reports the `probe: status=<code> accepted-permissions=<value|absent> run=<id>` line — what the factory's GitHub credential may read on the run's logs endpoint; submitted by hand when the digest comes back useless (td573240). Params: `todo_id`, optional `probe`, `run_id`, `target_node`. Refused while the factory kill switch is on. Needs host capabilities `repo_checkout`, `git` (pinned to the release owner's host) |
| `ci_rerun`          | `ssh_node`      | Dark release pass, the `validated:` non-repro path (gr588028): a release-fix session ending `validated:` with no commit parks the fix node on `waiting-for:ci-rerun` and the tick mints this job — `scripts/ci-rerun <run_id>` re-fires the newest red check.yml run's failed jobs on the cut's sha, once per run id (a second `validated:` on the same run falls to the `no_commit` path); exit 0 settles ~5 min for GitHub to flip the run to in_progress, then the plan's own `ci_verdict_poll` records the rerun's verdict. A 403 fails naming `scripts/ci-rerun --probe` — the rerun endpoint needs Actions write where the digest needed only read. Params: `todo_id`, optional `target_node` (a supplied `run_id` is refused — only `--probe` takes one). Refused while the factory kill switch is on. Needs host capabilities `repo_checkout`, `git` (pinned to the release owner's host) |

## Dark release-pass starter — release_poll and the stuck watchdog

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `release_poll`      | `job_inproc`    | Release loop starter + observation-only stuck watchdog: first, at any hour, it judges the `release.round` row's `state_at` against the `release.stage_deadlines` JSON (per-stage minutes; a stage absent/non-integer/`<= 0` unwatched, an unparsable value watches nothing) and files at most one `ask-user` ruling todo per `(round, stage)` (tag `release-stuck:r<N>:<stage>`) naming only the hand verbs — deploys, supersedes and cuts stay human. Then mints one `release-pass` plan only when its gates pass — the clock is outside `release.no_release_windows` (comma-separated UTC `HH:MM-HH:MM` pairs, half-open `[start, end)`, a start later than the end crosses midnight; default `03:00-04:20`, the nightly backup window), no `release-pass`-tagged todo is open, the shared `release.round` record names no open round (absent is "no round", unreadable blocks), GitHub `compare/prod...main` reports `ahead_by > 0` with `behind_by == 0` (`diverged`/`behind` never mints), no `release/r<N>` head is in flight (in flight unless its `deployed/r<N>` tag exists AND prod contains the head — or the head is already on main, a stale superseded ref), and no `release-main:<sha12>`-tagged root already claims main's head — the claim check and the mint are one transaction under an advisory lock, so a new pass needs main to move. Else it writes one `blocked — <why>` line and mints nothing. Params: optional `dry_run`, `repo`. Armed by one recurring todo — ask a human operator (recipe: `docs/runbooks/dark-factory-arming.md`; check for an untagged pre-tag open pass root before the first arm). Needs no host capability; the kill switch covers the watchdog's ask write (it files nothing, only a `would file` note) — the mint itself is not covered: it only mints, the plan runs when `plan_dispatch` is armed |

## See also

- [[precis-job-factory-help]] — run_slice / land_slice / plan ticks / PR lanes
- [[precis-job-help]] — submit shape, `args=` channel, the dispatch recipe
- [[precis-job-types-help]] — the non-factory `job_type` registry
- [[precis-job-ops-help]] — cancel / retry / STATUS vocabulary
- [[precis-job-find-help]] — read the queue and a job's card

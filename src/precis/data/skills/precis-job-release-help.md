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
dispatch recipe in [[precis-job-help]]. The lanes run only while an
operator has armed them, and are refused while the factory kill switch
is on.

## Dark release-pass job types — the cut/poll/deploy lanes

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `release_cut`       | `ssh_node`      | Dark release pass, the `cut` node of a `release-pass` plan: cuts `release/r<N>` from main and returns without waiting for CI; the job stamps `meta.release = {rN, sha, range, repo}` on the plan root and the real range on the plan's `tests-green` gate and loop. A cut that does not happen (already live, a release in flight elsewhere) fails the job. Params: optional `todo_id`, `target_node`. Refused while the factory kill switch is on, or while a reviewer on an engine-stamped `land_slice` job is open or its FIX is unlanded (`release_poll` runs the same check before minting) |
| `ci_verdict_poll`   | `job_inproc`    | Dark release pass, the `poll` node: reads check.yml's GitHub Actions run on the cut's sha and records green/red in `release.ci_verdicts`, which the `tests-green` gate reads. The gate legs are the verdict: green as soon as `lint` and every `gate-fast` leg passed, without waiting for the soak legs of the same run, and a red soak does not make the release red. While those legs run the job parks itself (`STATUS:waiting_time`) and re-polls every 10 min, every 2 min once the run is 8 min old. With `restamp: true` (the release-fix loop's `restamp` node, after the fix nodes) it reads no verdict: it moves `meta.release` onto `release/r<N>`'s current head (sha, range end, the gate and the loop's `until`; no cut), so the next poll and digest key on the fixed head. It also records each fixer's `steer:` line from its newest handoff in the job summary (`td<id> steer: …`). Params: `todo_id` (reads the plan root's `meta.release`) or `sha` + optional `range`; optional `repo` (else `release.repo`, else the checkout's origin), `restamp` |
| `release_deploy`    | `ssh_node`      | Dark release pass, the `deploy` node: deploys the plan's cut pinned and records `release.prod_head`. Refused unless the approval ask blocking the node is answered `deploy r<N>` for that cut — Reto's answer, or the tick's auto-answer when auto-deploy is on and the exact sha has green CI with an open `release.round` on that head. Also refused while `release/r<N>`'s live head is not the sha the plan recorded: a push after the last verdict (a carried corrective) makes `release_poll` restamp the plan and poll the new head first, and a red there gets fix nodes like any other. Params: `todo_id`, optional `target_node`. Refused while the factory kill switch is on |

## Dark release pass — the confirm and merge-back lane

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `release_confirm`   | `ssh_node`      | Dark release pass, the `confirm` node after `deploy` (`retries: 0`): attests the deployed runtime — every live worker's heartbeat `code_sha`, plus the web's boot sha when a health URL is configured (unset means the web is not attested; the workers decide). On pass it confirms the deploy, closes the round and stamps the evidence on the plan root and `release.round`; a failed close still succeeds (the release is already `deployed/r<N>`). A pinned sha that is not the plan's recorded release sha still confirms when it IS the live `release/r<N>` head with a green gate run — a recorded `release.ci_verdicts` green or check.yml's newest run on the branch — because the branch can move past the plan sha after its verdict with no restamp (gr684358); anything unverifiable stays a refusal. The shared session MCP is attested from the `release.mcp_served` stamp the server writes itself (served sha, plus the checkout's sha once its restart is pending): no live stamp attests nothing and is no problem; a stamp still on another sha parks the probe, then files an ask to run `scripts/host-ops/pin-prod` (gr685985). Any other evidence mismatch or gap fails the job before anything is confirmed and the tick's retry-cap ask goes to a human — no rollback, no `good/r<N>` tag. With `merge_back: true` (the pass's `merge_back` node, also after `deploy`) it instead merges `origin/release/r<N>` into main as `Merge release/r<N> fix forward` and pushes main with the factory credential: no commit past the cut is a no-op; a real conflict pushes nothing and mints one resolver slice (lands on main normally) plus a second `merge_back` node behind it with `strategy: "ours"` and `resolver: <todo id>`, whose `git merge -s ours` records the release head as an ancestor of main once the resolver landed; the pass root stays open until both close. A `release/r<N>` already retired on origin (round close deletes it once its head is on main) is the same no-op when the recorded head is already on main, merges on the recorded sha while it is still fetchable, and only fails when no usable head remains. Params: `todo_id`, optional `target_node`, `merge_back`, `strategy`, `resolver`. Refused while the factory kill switch is on |

## Dark release-fix loop — the digest and rerun lanes

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `ci_red_digest`     | `ssh_node`      | Dark release pass, the `digest` node of the `release-fix` loop body: digests the newest red check.yml run on the cut's sha and posts it — secret-masked, capped — as one `brief:` comment on each `run_slice` todo `blocked-by` the digest node (the fixer's brief). When the pass root or the fix loop carries a `brief:` comment, the newest at each scope is quoted at the head of that digest as a standing brief from the release owner, with the order to report `steer: followed` or `steer: not followed because …` in the fixer's handoff. A digest whose failing ids parse complete is split by the failing `src` module its assertion or import line names, else by test file (at most 8 clusters): one fix node per cluster, the extra ones minted beside the iteration's own, each landing on `release/r<N>`. A test red of 1-2 ids that passes the hard-stop lists may instead be waived (when red-waiver is on, the default): a `broken-build` gripe, the verdict recorded green-waived, no fix session. With `params.probe` + `params.run_id` instead reports what the factory's GitHub credential may read on that run's logs endpoint (`probe: status=<code> accepted-permissions=<value|absent> run=<id>`). Params: `todo_id`, optional `probe`, `run_id`, `target_node`. Refused while the factory kill switch is on |
| `ci_rerun`          | `ssh_node`      | Dark release pass, the `validated:` non-repro path (gr588028): a release-fix session ending `validated:` with no commit parks the fix node on `waiting-for:ci-rerun` and the tick mints this job — re-fires the newest red check.yml run's failed jobs on the cut's sha, once per run id (a second `validated:` on the same run falls to the `no_commit` path); a successful rerun settles ≈5 min for GitHub to flip the run to in_progress, then the plan's own `ci_verdict_poll` records the rerun's verdict. A 403 fails — the rerun endpoint needs Actions write where the digest needed only read. Params: `todo_id`, optional `target_node` (a supplied `run_id` is refused). Refused while the factory kill switch is on |

## Dark release-pass starter — release_poll and the stuck watchdog

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `release_poll`      | `job_inproc`    | Release loop starter + observation-only stuck watchdog: first, at any hour, it files at most one `ask-user` ruling todo per `(round, stage)` when the `release.round` record has sat in a stage past that stage's deadline, naming only the hand verbs — deploys, supersedes and cuts stay human. Then mints one `release-pass` plan only when its gates pass — the clock is outside the no-release windows (default `03:00-04:20` UTC), no `release-pass`-tagged todo is open, the shared `release.round` record names no open round, main is ahead of prod and not diverged or behind, no `release/r<N>` head is in flight (in flight unless its `deployed/r<N>` tag exists AND prod contains the head — or the head is already on main), and no `release-main:<sha12>`-tagged root already claims main's head, so a new pass needs main to move. Else it writes one `blocked — <why>` line and mints nothing. While a pass is open it also keeps that pass on its release branch's head: when the branch moved after the pass's last verdict and before its deploy started, it moves `meta.release` onto the new head (the `tests-green` gate reopens), reopens the fix loop and mints one `poll` under it; no fix iteration is counted. Params: optional `dry_run`, `repo`. The kill switch covers the watchdog's ask write (it files nothing, only a `would file` note) — the mint itself is not covered: it only mints, the plan runs when `plan_dispatch` is armed |

## Reading a release pass — one call

`get(kind='todo', id=<pass root or any node in it>, view='release')` prints the pass on one screen from stored data only (no git, no network): the header (`r<N>`, branch, cut and head sha, cut host), the state, the newest CI verdict on the head, `head on main` / `on prod` (`unknown` when no record says), one line per node in pass order with its newest job and age, an indented `held:` line (the deploy's stored refusal and the review units it waits on, keyed `td<id>` or `sha:<sha12>`), and `asks:` with who must answer. A todo outside a pass is refused; `view='tree'` reads any subtree.

## See also

- [[precis-job-factory-help]] — run_slice / land_slice / plan ticks / PR lanes
- [[precis-job-help]] — submit shape, `args=` channel, the dispatch recipe
- [[precis-job-types-help]] — the non-factory `job_type` registry
- [[precis-job-ops-help]] — cancel / retry / STATUS vocabulary
- [[precis-job-find-help]] — read the queue and a job's card

---
id: reland-slice
family: work
title: precis — re-land a factory slice by hand
summary: the hand re-land of a factory slice whose land_slice job went red outside its diff — triage infra red against real red, answer the plan ask first, else clear the child-failed tag and mint the land yourself, plus the local pre-land check for a branch that cannot be tested before main
answers:
  - a land_slice job is red with no failing assertion — what do I do?
  - how do I tell an infra red from a real red on a slice land?
  - why does my hand-minted land_slice job sit queued and never run?
  - how do I re-mint land_slice for a slice todo by hand?
  - how do I test a slice branch locally before its quick land?
applies-to: land_slice jobs under a slice todo (dark-factory plans)
status: active
tags: [workflow]
kinds: [todo, job]
scope: write
governs: factory
---

# reland-slice — land a slice again by hand

Trigger: a `land_slice` job failed with no assertion naming the diff, or on a
test that is green on main and names no path the diff touches. The lander and
the plan tick already absorb most infra reds; this is what is left to you.

## Triage

Read the red job (`get(kind='job', id=<red land>)`): its params and log tail.

- **Infra red** — exit 137, `worker 'gwN' crashed`, a container or network
  error before lint, a wall-deadline kill. The lander retries gate kills
  itself, then fails the job attempt-free and the tick parks the todo behind
  an ask offering `land again (no builder)`. **Answer that ask** — the tick
  re-lands the same branch with the failed job's params. Re-land by hand only
  when no such ask exists or the tick cannot (below).
- **Real red** — lint, a test the diff reaches, a merge conflict. Not this
  skill: post a `brief:` comment on the slice todo naming the failures
  (suspects as suspects) and answer the retry-cap ask with its retry option.

A deadline-killed land may already have merged: check the todo's `landed:`
comment or origin/main before landing again.

## Hand re-land

1. Copy `branch`, `ref`, `head` and `target_node` (and `lane`, `todo_id`,
   `repo`, if present) from the red job's params. A red land leaves the
   work in the build host's hub, so `ref` still holds `head`, and
   `target_node` must stay that host (the todo's `meta.build_ref.host`).
   Run the diff's cheap parity tests
   yourself before going on; a quick land that reddens main is the next
   tree's problem.
2. Clear the failure bubble. The red land tagged the todo
   `child-failed:<job>`, and a child-failed tag blocks the claim of every
   job under the todo: a job minted over it sits `STATUS:queued` with no log.

   ```
   tag(kind='todo', id=T, remove=['child-failed:<job>'])
   ```

3. Mint the land. A terminal prior job with the same idem key allows a fresh
   one; `--quick` is the default lane.

   ```
   put(kind='job', text='land_slice td<T> --quick',
       args={'parent_id': T, 'job_type': 'land_slice',
             'idem_key': 'land_slice:td<T>',
             'params': {'branch': 'slice/<T>', 'todo_id': T,
                        'ref': '<ref>', 'head': '<sha>',
                        'target_node': '<node>'}})
   ```

4. A queued job you no longer want: `tag(kind='job', id=N,
   add=['STATUS:cancel_requested'])` (a queued job cancels directly).
5. Hold any open retry-cap ask until the hand land succeeded, then close it
   as moot. Answering it earlier re-arms the node, and a builder could start
   a new attempt of the task while the land runs.
6. On green, land notification marks the todo `landed:` + `STATUS:done`.

## Local pre-land check

A slice carrying a migration or a harness file cannot run tests before main:
lands are lint-only and the release gate runs the suite. To test the branch
first, bring it local from the build host's hub (the todo's
`meta.build_ref`: `host`, `ref`, and `head`, the commit a land takes):

1. On that host, the hub is `precis-hub.git` in the checkout's git common
   dir: `git --git-dir <hub> log --oneline main..<ref>`. A top commit
   `wip: snapshot` is unfinished work above `head`.
2. Bundle the commits and stream them to a local file over ssh:
   `git --git-dir <hub> bundle create - main..<ref>`.
3. Locally, fetch `head` into a scratch branch and add a worktree:

   ```
   git fetch <bundle> <ref>
   git branch <scratch> <head>
   git worktree add .claude/worktrees/<scratch> <scratch>
   ```

4. Run the branch's tests plus `tests/test_schema_baseline.py`,
   `tests/test_mcp_raise_next_ratchet.py` and
   `tests/test_dispatch_args_keys.py` through that tree's own
   `scripts/test`. A new `args` verb must also be listed in that last test's
   passthrough roster, or main goes red.
5. To edit or commit there, enter the scratch worktree as the session's
   working tree (Claude Code: `EnterWorktree(path=…)`); exiting returns to
   the primary checkout, so re-enter your own tree before anything else.
6. Land from the scratch tree with `scripts/ship --quick "<subject>
   (td<T>)"`; the todo handle in the subject is what moves the todo. Then
   remove the scratch worktree and branch (`git worktree unlock` first).

## See also

- [[precis-job-help]] — minting, tagging and cancelling jobs.
- [[precis-job-factory-help]] — the factory's job types and ladder.
- [[gripe-fleet]] — the builder-wave mechanics around a landed branch.

---
id: precis-fix-gripe-help
title: precis — drive a gripe to a fix landed on main
summary: fix_gripe job recipe (lane OFF since 2026-10-02, hand-submit only) — gripe to job to a squash commit on main, iteration, review
answers:
  - who fixes a gripe now that the fix lane is off?
  - how do I hand-submit a fix_gripe job (reference only)?
  - how do I check whether my gripe-fix job is done?
  - how do I review the candidate fix's diff before merging?
  - how do I reject a fix and ask for another pass?
  - how do I cancel a fix job that's stuck?
applies-to: put (kind='job', job_type='fix_gripe')
tags: workflow, troubleshooting
kinds: job, gripe, todo
status: active
---

# precis-fix-gripe-help — get a gripe fixed end-to-end

**The fix lane is OFF (Reto, 2026-10-02).** Nothing mints `fix_gripe`
jobs any more, and a hand-submitted one skips at the preflight unless
the worker node holds a push credential. Gripes are fixed by the
owning thread's session in its own worktree; do not queue a job to
get a gripe fixed. The job type and this recipe stay as reference for
a deliberate hand-submit.

Recipe for handing a gripe to an agent, getting its fix landed
on `main`, and iterating until the fix holds. Joins `precis-gripe-help` (the bug tracker) and
`precis-job-help` (the offline-work substrate).

## Who fixes a gripe now that the lane is off?

The repo session that owns the gripe's work thread, in its own
worktree. Unthreaded
gripes are relinked to a thread by `/pillar-review`. The
automatic `diagnose_gripe` pass is off too; a hand-`put`
`diagnose_gripe` job still runs and comments a `DIAGNOSIS`.

## How do I hand-submit a fix_gripe job (reference only)?

**Slice-5 canonical pattern — write the intent as a todo; the
dispatch worker mints the job under it.**

```python
# 1) Create the intent under whichever strategic owns code
#    quality (or a one-off if there's no strategic home).
parent_id = put(
    kind="todo",
    text="Fix gripe:42 (rate-limit edge case)",
    parent_id=engineering_hygiene_strategic_id,
    meta={"executor": "claude_inproc", "job_type": "fix_gripe"},
)  # → returns the new ref

# 2) Link the todo to the gripe so the lineage is queryable.
link(kind="todo", id=parent_id, target="gripe:42", rel="fixes")

# 3) Walk away. Within ~1 minute the dispatch worker mints a
#    kind='job' under the todo; claude_inproc claims it and
#    runs the fix; on success the parent todo auto-flips to
#    STATUS:done via meta.auto_check={'type':'child_job_succeeded'}
#    (auto-injected by the dispatcher).
```

**Ad-hoc submit** (skip the todo layer — useful for one-off
direct submits):

```python
put(kind='job',
    parent_id=<some_todo_id>,         # required — orphan jobs rejected
    job_type='fix_gripe',
    link='gripe:42', rel='fixes')
# → created job id=101
# gripe auto-tagged STATUS:ready_for_fix as a side effect.
```

One call. The worker clones the repo, runs `claude -p` on a
`gripe_42` branch, lands the agent's commits on the upstream's
`main` as one squash commit, and posts the landed sha on the
gripe. The fix lane pushes straight to main (Reto, 2026-10-01);
the gate is downstream: check.yml runs on every push to main, and
`origin/gated` only moves past the commit on a green full gate.

## What happens when a fix fails?

* `STATUS:failed` on the job + a `job_event` chunk with the reason.
* `child-failed:<job_id>` tag bubbles to the parent todo. The
  doable view skips the parent until the flag is cleared.
* The nursery digest surfaces the stuck parent.
* The parent's owner (asa-bot) decides next move — see
  "Re-submit a failed job" in `precis-job-help`.

**No auto-retry.** The substrate refuses to multiply attempts
silently; you (or asa-bot) make the retry call explicitly.

## Which repo does the agent operate on?

The worker picks the repo from the gripe's `repo:<name>` tag.
The set of allowed names is configured on the deployment side
(`PRECIS_FIX_REPOS` JSON map). If the gripe carries no `repo:`
tag, the worker falls back to the single-repo default
(`PRECIS_FIX_REPO_DIR`).

If the linked gripe carries a `repo:` tag that isn't in the
allowlist, the `put(kind='job', ...)` call is rejected at submit
time with a clear message — no zombie queued jobs.

Tag the gripe before submitting if you need a non-default repo:

```python
tag(kind="gripe", id=42, add=["repo:my-other-project"])
put(kind="job", job_type="fix_gripe", link="gripe:42", rel="fixes")
```

## How do I check whether my gripe-fix is done?
## Has the fix worker finished yet?

```python
search(kind="job", link="gripe:42")
# most recent first; check STATUS on the top result
```

Or look at the gripe — it transitions to `STATUS:in_review`
once a fix has landed on main (landed, not yet verified).

An agent that finds the defect already gone makes no commit and
ends on an `ALREADY FIXED: <evidence>` line. The job then succeeds
with nothing landed, and the gripe goes to `STATUS:in_review` with
the evidence as a comment — verify it and close. This holds only on
a clean finish; an agent cut off by `max_turns` still fails.

## Where does the fix land?
## How do I see the fix?

On `main` of the upstream the worker's repo checkout points at,
as one squash commit whose subject is the agent's last commit
subject plus `(gr<id>)`. The gripe comment names the sha:

```bash
git fetch
git show <sha>
```

How the land works — the same protocol `scripts/ship` uses:
fetch current main, re-apply the agent's change on top of it
(`git merge-tree`), commit it with current main as sole parent,
push **without force**. A non-force push is accepted only as a
fast-forward, so if anything landed meanwhile the push is refused
and the worker re-syncs and retries (3 attempts); it can never
overwrite a concurrent land. A conflict with current main lands
nothing and fails the job.

**A job reports success only once `git ls-remote` confirms main
on the upstream contains the commit** (gr458326 — an exit-0 push
once reached a directory on the worker node and nothing else).
A land that fails leaves the gripe `open`; the commit is kept as
branch `gripe_<id>` in the worker's own checkout, and the failure
text says where.

If the worker cannot publish at all, jobs **skip** instead — a
`git push --dry-run` runs before the agent does, so the run
costs one round trip rather than a full agent budget, and the
gripe keeps its retry budget. A skip saying "cannot publish a
branch to …" is a credential question for the deployment, not
a bug in the run and not something re-running will fix.

## Review the fix
## Look at the diff

`git show <sha>` with the sha from the gripe comment. The fix is
already on main: a bad one is reverted, not "not merged".

## Accept the fix
## Close the gripe

Once check.yml on main is green for the landed commit and the
fix holds:

```python
put(kind="gripe", id=42, text="verified: <sha> fixes it")
delete(kind="gripe", id=42)
```

## Reject the fix and ask for another pass
## Iterate on a half-done fix

Append a comment describing what's wrong; re-submit:

```python
put(
    kind="gripe",
    id=42,
    text="wrong approach — the issue is the chunker, not the search verb",
)
put(kind="job", job_type="fix_gripe", link="gripe:42", rel="fixes")
```

The new job sees the new comment because the worker re-reads
the gripe's timeline at job-start. Each attempt is a fresh
clone + fresh branch — no leftover state from the prior
attempt.

## My fix job failed — what now?
## What if claude can't fix the bug?

Read the failure comment on the gripe (most recent
`gripe_comment`). The worker explains what went wrong. Add a
clarifying comment and re-submit, or escalate to a human via a
`todo`:

```python
put(
    kind="todo",
    text="Manual fix needed for gripe:42 — agent can't reach upstream",
    link="gripe:42",
    rel="resolves",
)
```

If the agent committed but the land failed, its work is branch
`gripe_<id>` in the worker's repo checkout
(`$PRECIS_FIX_REPO_DIR`); the scratch clone is removed once the
branch is copied there. If the agent itself failed, the clone
under `$PRECIS_FIX_WORK_DIR/clones/gripe_<id>` is retained until
the next attempt for that gripe.

## My fix job is stuck or running too long — cancel it
## Kill a hung fix attempt

```python
tag(kind="job", id=101, add=["STATUS:cancel_requested"])
```

Worker SIGTERMs the subprocess at the next safe point; final
status is `STATUS:cancelled`. The clone dir is preserved.

## What if I submit two fix_gripe jobs at once?

The dispatcher dedupes by `idem_key = link target`. A second
`put` with the same `link='gripe:42'` returns the in-flight
job's id while it's still queued/running. Once the prior is
terminal, a fresh job is created.

No accidental fan-out.

## See also

- [[precis-gripe-help]] — the bug tracker
- [[precis-job-help]] — jobs in general

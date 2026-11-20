---
id: precis-fix-gripe-help
family: work
title: precis — the fix_gripe job (switched off): reading existing jobs
summary: reading an existing fix_gripe job — find it, where its fix landed, failure, cancel, close the gripe; nothing mints new ones, the live routes for fixing a gripe are in precis-gripe-help
answers:
  - how do I check whether a fix_gripe job is done?
  - how do I review what a fix_gripe job landed?
  - how do I cancel a fix_gripe job that's stuck?
applies-to: search/tag (kind='job', job_type='fix_gripe')
tags: workflow, troubleshooting
kinds: job, gripe, todo
status: active
scope: write
---

# precis-fix-gripe-help — the fix_gripe job (switched off)

**Nothing mints `fix_gripe` jobs** (`backlog_groom`, the pass that did, is switched off). Do not queue one to get a gripe
fixed: [[precis-gripe-help]] § "Who fixes a gripe?" names the live
routes. This page is for reading `fix_gripe` jobs that already exist.

## What happens when a fix fails?

* `STATUS:failed` on the job + a `job_event` chunk with the reason.
* `child-failed:<job_id>` tag bubbles to the parent todo. The
  doable view skips the parent until the flag is cleared.
* The parent's owner decides the next move — see "Re-submit a
  failed job" in `precis-job-ops-help`.

**No auto-retry.** The substrate refuses to multiply attempts
silently; the retry call is explicit.

## How do I check whether my gripe-fix is done?
## Has the fix worker finished yet?

```python
search(kind="job", link="gripe:42")
# most recent first; check STATUS on the top result
```

Or look at the gripe — it sits at `STATUS:in_review` while a fix is
in flight (claimed through landing and deploy). Once a confirmed
deploy carries the fix it moves to `STATUS:auto_verify`, where the
deploy check closes it `done` or sends it back to `triaged`.

An agent that finds the defect already gone makes no commit and
ends on an `ALREADY FIXED: <evidence>` line. The job then succeeds
with nothing landed, and the gripe goes to `STATUS:in_review` with
the evidence as a comment — verify it and close. This holds only on
a clean finish; an agent cut off by `max_turns` still fails.

## Where does the fix land?
## How do I see the fix?

On `main`, as one squash commit whose subject is the agent's last
commit subject plus `(gr<id>)`. The gripe comment names the sha. A job
reports success only once the commit is on main; a land that fails
leaves the gripe `open` and the failure text says why. A conflict with
current main lands nothing and fails the job. A skip saying "cannot
publish a branch to …" is not a bug in the run and re-running will not
fix it.

## Review the fix
## Look at the diff

The sha from the gripe comment names the commit. The fix is already on
main: a bad one is reverted, not "not merged".

## Accept the fix
## Close the gripe

Once the checks on main are green for the landed commit and the fix
holds:

```python
put(kind="gripe", id=42, text="resolved: <sha> (r<N>) — <how verified>",
    tags=["STATUS:done"])
```

The comment must come first and start with `resolved:`; the tag is refused
otherwise (the error names the put to make). Won't-fix is
`resolved: wontfix — <reason>` plus `STATUS:wontfix`. `delete` is only for
duplicates, spam and gripes filed in error.

## My fix job failed — what now?
## What if claude can't fix the bug?

Read the failure comment on the gripe (most recent `gripe_comment`);
it explains what went wrong. Add a clarifying comment, or hand it to a
human via a `todo`:

```python
put(
    kind="todo",
    text="Manual fix needed for gripe:42 — agent can't reach upstream",
    link="gripe:42",
    rel="fixes",
)
```

## My fix job is stuck or running too long — cancel it
## Kill a hung fix attempt

```python
tag(kind="job", id=101, add=["STATUS:cancel_requested"])
```

The run is stopped at the next safe point; final status is
`STATUS:cancelled`.

## See also

- [[precis-gripe-help]] — the bug tracker
- [[precis-job-help]] — jobs in general

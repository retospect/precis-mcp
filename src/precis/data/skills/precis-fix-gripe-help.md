---
id: precis-fix-gripe-help
title: precis — drive a gripe to a candidate fix branch
applies-to: put (kind='job', job_type='fix_gripe')
status: active
---

# precis-fix-gripe-help — get a gripe fixed end-to-end

Recipe for handing a gripe to an agent, getting a candidate fix
branch back on `origin`, and iterating until the fix is good
enough to merge. Joins `precis-gripe-help` (the bug tracker) and
`precis-job-help` (the offline-work substrate).

## I want this gripe fixed
## Auto-fix this bug
## Get an agent to prepare a fix branch for me

```python
put(kind='job', job_type='fix_gripe', link='gripe:42', rel='fixes')
# → created job id=101
# gripe auto-tagged STATUS:ready_for_fix as a side effect.
```

One call. The worker clones the repo, runs `claude -p` on a
`gripe_42` branch, pushes the branch to `origin` (the source
repo), and posts a comment on the gripe when it's ready for
review.

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
tag(kind='gripe', id=42, add=['repo:my-other-project'])
put(kind='job', job_type='fix_gripe', link='gripe:42', rel='fixes')
```

## What the fix worker actually does

1. Reads the gripe body + current `gripe_comment` thread.
2. Clones the source repo to `$PRECIS_FIX_WORK_DIR/clones/
   gripe_<id>` and checks out a fresh `gripe_<id>` branch.
   Independent `.git`; the source repo's `main` is untouched.
3. Runs `claude -p --dangerously-skip-permissions` as a
   subprocess of the precis worker with `cwd` = the clone dir
   and a restricted env (no DB creds; `~/.claude` mounted for
   auth).
4. On success: commits land on the branch, the worker pushes
   `gripe_<id>` to `origin`, posts a `gripe_comment` with the
   SHA and fetch instructions, tags gripe `STATUS:in_review`,
   removes the clone.
5. On failure: posts a comment with the stderr tail, rolls the
   gripe back to `STATUS:open`, **keeps the clone dir** so a
   human can `cd` into it and inspect what the agent left
   behind.

## How do I check whether my gripe-fix is done?
## Has the fix worker finished yet?

```python
search(kind='job', link='gripe:42')
# most recent first; check STATUS on the top result
```

Or look at the gripe — it transitions to `STATUS:in_review`
once a fix attempt lands cleanly.

## Where does the candidate branch live?
## How do I fetch the fix?

In `origin` of the source repo (where `main` lives). The fetch
instructions are in the gripe comment the worker posted; in
your normal working repo:

```bash
git fetch
git checkout gripe_42
git diff main..gripe_42
```

The clone dir under `$PRECIS_FIX_WORK_DIR/clones/` is removed
on success — the branch in origin is what survives.

## Review the candidate fix
## Look at the diff

Standard git workflow. The worker posts the SHA in its
gripe_comment so you can verify which commit you're looking at.

## Accept the fix
## Merge the fix and close the gripe

Merge the branch in your normal flow. Once merged:

```python
put(kind='gripe', id=42, text='merged in <sha>')
delete(kind='gripe', id=42)
```

## Reject the fix and ask for another pass
## Iterate on a half-done fix

Append a comment describing what's wrong; re-submit:

```python
put(kind='gripe', id=42, text='wrong approach — the issue is the chunker, not the search verb')
put(kind='job', job_type='fix_gripe', link='gripe:42', rel='fixes')
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
put(kind='todo',
    text='Manual fix needed for gripe:42 — agent can\'t reach upstream',
    link='gripe:42', rel='resolves')
```

The clone dir is retained on failure (under
`$PRECIS_FIX_WORK_DIR/clones/gripe_<id>`) so you can `cd` into
it and see exactly what the agent left behind.

## My fix job is stuck or running too long — cancel it
## Kill a hung fix attempt

```python
tag(kind='job', id=101, add=['STATUS:cancel_requested'])
```

Worker SIGTERMs the subprocess at the next safe point; final
status is `STATUS:cancelled`. The clone dir is preserved.

## Where is the fix worker running?
## Do I need to start anything?

The `claude_inproc` runner is part of the standard `precis
worker` round-robin and runs inside the precis container.
Deployment requirements:

- `PRECIS_FIX_REPO_DIR` env var pointing at the canonical
  precis-mcp repo (host path), bind-mounted into the precis
  container at the same path.
- `PRECIS_FIX_WORK_DIR` env var, same bind-mount pattern.
- `~/.claude` bind-mounted (rw) so claude's session tokens can
  refresh.
- Precis image includes the `claude` binary.

With those set, `precis worker` picks `job_claude_inproc` up
automatically. To run only this one runner:

```bash
precis worker --only job_claude_inproc
```

## Trust model
## Is it safe to run claude with --dangerously-skip-permissions?

The precis runtime trusts the agent within its container. The
failure boundary is `cwd` (the clone dir) plus a restricted env
that strips DB credentials. A pre-push hook in every clone
rejects pushes to any branch not matching `gripe_*`, so claude
can't push over `main`.

This is **not** a hard sandbox — it's a trust boundary that
matches the rest of the precis trust model. If the threat
profile changes, swap in a per-job docker container (planned
under the future `claude_docker` executor).

## What if I submit two fix_gripe jobs at once?

The dispatcher dedupes by `idem_key = link target`. A second
`put` with the same `link='gripe:42'` returns the in-flight
job's id while it's still queued/running. Once the prior is
terminal, a fresh job is created.

No accidental fan-out.

## See also

```python
get(kind='skill', id='precis-gripe-help')   # the bug tracker
get(kind='skill', id='precis-job-help')     # jobs in general
```

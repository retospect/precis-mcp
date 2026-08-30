---
id: precis-fix-gripe-help
title: precis — drive a gripe to a candidate fix branch
summary: end-to-end bug fix recipe — gripe to job to candidate branch, iteration, review handoff
answers:
  - how do I get an agent to prepare a fix branch for a bug I filed?
  - how do I check whether my gripe-fix job is done?
  - how do I review the candidate fix's diff before merging?
  - how do I reject a fix and ask for another pass?
  - how do I cancel a fix job that's stuck?
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
`gripe_42` branch, pushes the branch to `origin` (the source
repo), and posts a comment on the gripe when it's ready for
review.

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
put(kind="gripe", id=42, text="merged in <sha>")
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

The clone dir is retained on failure (under
`$PRECIS_FIX_WORK_DIR/clones/gripe_<id>`) so you can `cd` into
it and see exactly what the agent left behind.

## My fix job is stuck or running too long — cancel it
## Kill a hung fix attempt

```python
tag(kind="job", id=101, add=["STATUS:cancel_requested"])
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
- **§13 container available (recommended)** — `PRECIS_AGENT_CONTAINER=1`
  on a host that can run the `precis-agent` image (§H cycle a: the image
  now carries git + uv so a cloned repo's own tests can run inside it).
  A containerized run is network-isolated and needs no operator ack.
- **`PRECIS_FIX_GRIPE_UNSANDBOXED_ACK=1`** — required ONLY when the §13
  container is unavailable on this host (feature off, or the
  capability probe fails). fix_gripe is **fail-closed** (gr179498) in
  that case: it refuses to fall back to running full-privilege and
  unsandboxed on verbatim (agent-filable) gripe text unless an
  operator explicitly acks the risk here. Without it a submitted job
  (or a `backlog_groom` auto-promotion) skips clean and the gripe
  stays open. Set it only on a trusted-operator deployment without the
  §13 container.

With those set, `precis worker` picks `job_claude_inproc` up
automatically. To run only this one runner:

```bash
precis worker --only job_claude_inproc
```

## Trust model
## Is it safe to run the fix agent unsandboxed?

Whenever the §13 container is available (`PRECIS_AGENT_CONTAINER=1`
on a capable host), fix_gripe's agent runs *inside* it: network-isolated
(`egress:api-only` — reaches only the Anthropic API, no DB, no open
egress), with ONLY the clone dir bind-mounted in — never the source repo.
The agent can commit inside the clone; it has no filesystem path to
origin and no network route to it, so it cannot push. That boundary is
real enough that a containerized run needs **no operator ack**.

When the container isn't available, the failure boundary falls back to
`cwd` (the clone dir) plus an isolated env that strips DB credentials —
same trust boundary as before §H, and **not** a hard sandbox. Because
the prompt embeds verbatim, agent-filable gripe text, that fallback
path is **fail-closed** behind `PRECIS_FIX_GRIPE_UNSANDBOXED_ACK`
(gr179498): the agent won't run unsandboxed without an explicit
operator ack — enforced by `call_claude_agent`'s
`require_container=not <ack>` — so enabling `backlog_groom` alone can't
feed attacker-shaped text into an unsandboxed run, even if a
containerized run was available a moment ago and then failed mid-run.

**Write-back is a commit, pushed on the trusted side.** In EITHER mode
(containerized or the fail-closed fallback) the agent never pushes —
it only commits inside the clone. Once the agent's run finishes, the
worker process itself (trusted, host-side, holding the real repo path
and no sandbox) performs the `git push`, guarded host-side to reject
anything not matching `gripe_<id>`. A pre-push hook in every clone
additionally rejects pushes to any branch not matching `gripe_*` —
belt and braces, not the only defense.

## What if I submit two fix_gripe jobs at once?

The dispatcher dedupes by `idem_key = link target`. A second
`put` with the same `link='gripe:42'` returns the in-flight
job's id while it's still queued/running. Once the prior is
terminal, a fresh job is created.

No accidental fan-out.

## See also

```python
get(kind="skill", id="precis-gripe-help")  # the bug tracker
get(kind="skill", id="precis-job-help")  # jobs in general
```

# fix_gripe worker deployment + trust model

**When.** You're standing up or auditing the `job_type='fix_gripe'`
runner on a host — deciding whether the §13 container is required, or
explaining to a reviewer why an unsandboxed run is (or isn't) safe.
Agent-facing usage of the fix_gripe recipe (submit, review, iterate,
cancel) lives in `get(kind='skill', id='precis-fix-gripe-help')`; this is
the operator/deployment half.

## Where is the fix worker running? Do I need to start anything?

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

## Trust model — is it safe to run the fix agent unsandboxed?

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

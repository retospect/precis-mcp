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
- **That checkout must be able to PUSH to its own upstream's `main`**, as
  whichever user the worker runs as. A fix is landed on `main` of
  `git remote get-url origin` of that checkout as one squash commit on
  current main, pushed without force (fast-forward only, so a concurrent
  land makes it re-sync and retry, never overwrite), and a job succeeds
  only once `git ls-remote` shows main containing the commit. Every
  post-agent git command runs in this checkout, never in the per-gripe
  clone, so the credential is never used where the agent could plant
  hooks or config. A pull-only checkout —
  an anonymous HTTPS clone, which is what the ansible role provisions —
  makes every job **skip**: a `git push --dry-run` runs from that checkout
  before the per-gripe clone is made or the agent spawned, so an
  undeliverable deployment costs one round trip — no agent run, no clone on
  disk, and the gripe keeps its retry budget. A skip also removes any clone
  an earlier attempt left for that gripe; clones left by gripes that are
  never retried need a manual sweep of `PRECIS_FIX_WORK_DIR/clones/`. Jobs
  skipping with "cannot publish a branch to …" mean this, and no amount
  of re-running will change it. That is the honest outcome, not a bug in
  the run, and it is a deliberate change from the behaviour that
  produced gr458326: pushing "to origin" from the per-job clone reached
  the local checkout, exited 0, and let 43 jobs report a delivery that
  never left the machine while parking their gripes at `in_review`.
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

**Write-back is a commit, landed on the trusted side.** In EITHER mode
(containerized or the fail-closed fallback) the agent never pushes —
it only commits inside the clone. Once the agent's run finishes, the
worker process itself (trusted, host-side) fetches the `gripe_<id>`
branch out of the clone into `PRECIS_FIX_REPO_DIR` and lands it on main
from there (Reto 2026-10-01, td459082: the lane pushes straight to main;
check.yml on main and `origin/gated` are the downstream gate). A pre-push
hook in every clone refuses any push from inside it — a tripwire, since
the agent can rewrite it; the boundary is the absent route and credential.

### Provisioning the push credential

The credential belongs to the deploy user on the agent-lane worker, in
its home directory — never in the repo or the job env. The containerized
agent cannot read it (only the clone is mounted). **An unsandboxed run
(`PRECIS_FIX_GRIPE_UNSANDBOXED_ACK=1`) runs the agent as that same user
and CAN read it** — so a write-to-main credential and the unsandboxed ack
must not be combined on one host. Recommended: a fine-grained
GitHub token scoped to this one repository with Contents read/write and
no Workflows permission (so a fix cannot edit `.github/workflows/`),
stored via `git config --global credential.helper store` in
`~/.git-credentials` (mode 600). The checkout's `https://` origin stays as
the ansible role provisions it. Verify as the deploy user from the
checkout: `git push --dry-run origin HEAD:refs/heads/gripe_0` exits 0.

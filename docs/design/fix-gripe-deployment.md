# fix_gripe deployment

The `fix_gripe` job_type runs `claude -p
--dangerously-skip-permissions` as a subprocess of the precis
worker inside the precis container. Three things must be true
about the container at runtime:

1. The `claude` binary is on `$PATH`.
2. The host's `~/.claude` directory is bind-mounted into the
   container so claude inherits the operator's session.
3. The source repo and a scratch root are bind-mounted so
   `git clone --local` + `git push origin gripe_<id>` can
   round-trip the branch.

## What's already in place

The `dev` Dockerfile target (`docker/Dockerfile`) installs
`@anthropic-ai/claude-code` and pins it via the
`CLAUDE_CODE_VERSION` build arg. `~/.claude` and
`~/.claude.json` are already bind-mounted into precis-dev (see
ADR 0011 and the `compose.yaml` precis-dev service definition).

The `runtime` target does **not** install claude. For v1 the
worker is expected to run from the dev image — the user's
single-host dev box is the deployment target. To run fix_gripe
in a production runtime image, add claude to the runtime stage
(mirror the dev-system block: nodejs + `npm install -g
@anthropic-ai/claude-code@<pin>`).

## Required env vars on the precis container

| Var                          | Required | Default                | Notes                                                  |
|------------------------------|----------|------------------------|--------------------------------------------------------|
| `PRECIS_FIX_REPO_DIR`        | yes      | —                      | Path to the precis-mcp source repo                     |
| `PRECIS_FIX_WORK_DIR`        | yes      | —                      | Root for `clones/gripe_<id>/`; growth bounded by GC    |
| `PRECIS_FIX_CLAUDE_BIN`      | no       | `claude`               | Resolved from container `$PATH`                        |
| `PRECIS_FIX_CLAUDE_MODEL`    | no       | `claude-opus-4-7`      | Passed to `claude -p --model`                          |
| `PRECIS_FIX_TIMEOUT_SECONDS` | no       | `1800`                 | Wall-clock cap; SIGTERM then SIGKILL                   |
| `PRECIS_FIX_CLONE_TTL_DAYS`  | no       | `14`                   | Clone-dir age-out (not yet wired; see TODO below)      |

## Required bind-mounts

Add to the precis service in `~/work/infrastructure/compose.yaml`:

```yaml
services:
  precis-dev:
    # … existing keys …
    environment:
      PRECIS_FIX_REPO_DIR: /home/precis/work/projects/code/precis-mcp
      PRECIS_FIX_WORK_DIR: /home/precis/precis-fix-work
    volumes:
      # …existing ~/.claude, ~/work bind-mounts …
      - ${HOME}/precis-fix-work:/home/precis/precis-fix-work
```

`PRECIS_FIX_REPO_DIR` resolves through the existing `~/work`
bind-mount; `PRECIS_FIX_WORK_DIR` needs its own mount because
the host directory may not exist yet (`mkdir -p
~/precis-fix-work` on first run).

## Trust model

The container is the failure boundary, not a hard sandbox.
`claude -p --dangerously-skip-permissions` is acceptable here
because:

- `cwd` is the clone dir, not the precis source.
- `env` strips `PG*` / `PRECIS_DATABASE_URL` /
  `PRECIS_*` (see `_restricted_env` in
  `src/precis/workers/job_types/fix_gripe.py`) so claude can't
  reach the postgres backing the precis runtime.
- A pre-push hook in every clone rejects pushes to anything not
  matching `gripe_*` (see `_install_prepush_hook` in the same
  file).
- The post-run verification confirms `origin/main` is unchanged
  from job-start.

If the threat model changes (untrusted gripes, e.g.), swap in
the future `claude_docker` executor under
`src/precis/workers/executors/`. The MCP-side surface and the
`fix_gripe` job_type stay identical; only the runner changes.

## Known follow-ups

- **Clone GC**: the runner doesn't yet implement the
  `PRECIS_FIX_CLONE_TTL_DAYS` mtime sweep; the env var is
  documented and accepted but unused. Worth wiring once disk
  pressure is observed.
- **Runtime image**: production deployments need claude in the
  `runtime` target (currently dev-only).
- **Multi-repo**: v1 is hard-coded to the precis-mcp repo via
  `PRECIS_FIX_REPO_DIR`. Multi-repo support is a `params.repo`
  field + allowlist away.

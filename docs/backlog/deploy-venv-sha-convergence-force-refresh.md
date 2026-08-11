---
status: ready
title: deploy — force git-sha refetch so venvs converge on same-version main moves
model: sonnet
---

# deploy — force git-sha refetch so venvs converge on same-version main moves

## Motivation / why
The worker venv install (`deploy/roles/precis_worker/tasks/provision.yml:77`)
runs `uv pip install --upgrade 'precis-mcp[…] @ git+…@{{ precis_worker_git_ref }}'`
(ref = `main`). `--upgrade` only reinstalls when the resolved **package
version** is newer. Since `pyproject` `version` (`8.30.2`) stays fixed across
most commits, once a node has *any* `8.30.2` build installed, a moved `@main`
git sha with the same version is **not refetched** — uv treats the requirement
as already satisfied. The node sticks on whatever sha it first installed.

The strict convergence assert (`redeploy-precis.yml`, "Assert each managed venv
matches its deployed ref") then compares the recorded `direct_url.json`
`commit_id` against the frozen deploy target and **reddens the whole deploy**,
even though the installed `src/precis` may be byte-identical to the target
(the sha delta can be deploy/-only files that never enter the pip package).

Observed 2026-08-11: after a mid-deploy sibling ship, `caspar` landed on
`00d97b48` (parent of the target `d9de1d46`; delta = deploy/ ansible files
only). Three subsequent deploys could **not** move it — same-version
`--upgrade` no-op'd every time. 5/6 nodes were exact; caspar stuck cosmetically.

## In scope
- Make the install always converge the venv to the pinned sha: add
  `--reinstall-package precis-mcp` (or uv's `--refresh-package precis-mcp`) to
  the provision install command so the git ref is re-resolved and the exact
  target sha is installed regardless of the unchanged version string.
- Same treatment for the embedder-venv install and any other
  `git+…@ref`-installed managed venv covered by the convergence assert
  (`/opt/precis/embedder-venv`, the collapsed-worker path in
  `20b-precis-worker-collapsed.yml`).

## Explicitly NOT in scope
- Loosening the convergence assert (keep it strict — exact sha is the goal).
- Changing the pinning mechanism / freeze semantics.
- Version-bump automation.

## Acceptance criteria
- A deploy where main moved by a **same-version** commit converges **every**
  managed venv to the pinned sha in one run — no residual node stuck on an
  older same-version sha, no cosmetic convergence-assert red.
- Re-running the deploy on an already-converged cluster is still a fast no-op
  (the force-refresh must not add a full reinstall when the sha already matches
  — gate on installed-sha ≠ target, or rely on uv's cache making a same-sha
  refetch cheap; verify wall-clock doesn't regress materially).

## Target + blast radius
- `deploy/roles/precis_worker/tasks/provision.yml` (worker + embedder venv
  installs), `deploy/playbooks/20b-precis-worker-collapsed.yml`,
  `deploy/redeploy-precis.yml` (convergence assert — unchanged, just satisfied).
- All managed venvs cluster-wide; verify against the same-version-move case.

## Open questions / decisions log
- `--reinstall-package` (pip-compat, always reinstalls that package) vs uv's
  `--refresh-package` (busts the cache, reinstalls only if the resolution
  changed) — prefer `--refresh-package precis-mcp` if it avoids a needless
  reinstall on already-converged nodes; confirm uv version on the nodes
  supports it, else fall back to `--reinstall-package`.

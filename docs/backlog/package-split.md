---
status: draft
title: package split — carve stable kernels out of the precis-mcp wheel (uv workspace first, separate repos only where cadence or secrecy demands)
prio: normal
model: opus
snooze-until: 2026-10-16
---

# package split — carve stable kernels out of the precis-mcp wheel

Discussion 2026-09-16 (Reto + agent, gentle-rolling-goose worktree). Reto:
"break out obvious packages that support stuff. Mainly stable tools, they
can be their own pips … It should have no real changes for at least 30
days." The clock started 2026-09-16; `snooze-until` above is the earliest
day code moves. Until then only the behaviour-neutral prep below is fair
game.

## Motivation / why

One hatchling wheel carries nine top-level packages (`precis`, `precis_web`,
`asa_bot`, `asa_slack`, `precis_chem`, `precis_bio`, `precis_pathway`,
`precis_estimate`, `precis_se`). Two pressures: (1) some engines must stay
private until a paper (autocatpath today, via a private git source +
`/opt/precis/wheels` find-links in `scripts/deploy`) or serve consumers
outside precis (hexfold, python_index); (2) the wheel is clumsy to deploy
and reason about. **Not** CI speed: 59% of the last 30 days' commits touch
core and only ~10% are se/kernel/web-only, so a same-repo split would not
shorten the gate (the 6-way shard already did, ed4c9ee9).

## Shape

- **uv workspace in the same repo → many wheels** is the default. Separate
  repos only for: private until release (catpath), external consumers
  (hexfold, python_index), or a genuinely different release cadence.
- `precis` is a regular package (not a namespace), so members become
  top-level `precis_cad`, `precis_structsolve`, … with a one-release
  re-export shim at the old path.
- Break-out order, most-to-least stable and least-coupled first:
  `python_index` → `structsolve` → `cad` → `structure` (+ `design`,
  blocktree) → `pcb` → the five entry-point plugins (`precis_chem`,
  `precis_bio`, `precis_pathway`, `precis_estimate`, `precis_se`), which
  already load via the `precis.handlers` / `precis.job_types` /
  `precis.migrations` / `precis.handle_codes` groups (`dispatch.py::_load_plugins`).
- **Never** split out: handlers, workers, store, taproot, nanopub, quest,
  export, ingest — they are the product, not a kernel.

## Behaviour-neutral prep (allowed before the snooze lifts)

1. An import-boundary test: kernels must not import `precis.store`/handlers;
   core must not import a plugin. Grandfather the one known core→plugin
   leak (`quest/figures.py` → `precis_pathway`) with an explicit allowlist
   so the test lands green and the leak is visible.
2. Split kernel tests into store-free vs store-backed so a member wheel can
   run its own suite (today store-free: cad 9/25, pcb 13/47, structure 4/16).
3. Decide the deploy channel for member wheels: git `#subdirectory=` in the
   `deploy/redeploy-precis.yml` install line vs the existing
   `/opt/precis/wheels` find-links (catpath precedent). `scripts/deploy` pins
   one sha across three venvs; a workspace keeps that invariant.
4. PyPI publishing has lapsed (`publish.yml` fires on `v*` tags; last tag
   v8.4.4, pyproject at 8.33.x; the cluster installs from git so nobody
   noticed). Decide whether members publish at all before minting names.

## Explicitly NOT in scope

Moving any module before 2026-10-16. Changing what the cluster installs.
Renaming `precis` itself.

## Acceptance criteria

Per member: its own `pyproject.toml` under the workspace, its store-free
tests pass in isolation, `uv build` yields a wheel, the old import path
still resolves for one release, and the cluster deploy installs the same
pinned sha for every venv.

## Target + blast radius

`pyproject.toml`, `uv.lock`, `scripts/deploy`, `deploy/redeploy-precis.yml`,
`check.yml`, every `from precis.<kernel>` import in the moved member's
consumers.

## Open questions / decisions log

- Which member first, and does python_index want its own repo (external
  consumers) rather than a workspace slot? Reto's call at the snooze date.
- Member wheel deploy channel (prep item 3).

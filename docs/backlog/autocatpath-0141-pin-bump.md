---
status: draft
title: Bump the autocatpath floor pin to 0.14.1 — end the per-deploy override wipe
prio: high
---

# Bump the autocatpath floor pin to 0.14.1 — end the per-deploy override wipe

> **BLOCKED 2026-08-21 — 0.14.1 does not exist as a release.** `gh release
> list --repo retospect/catpath` tops out at **0.13.0** (2026-08-12); the
> 0.14.1 that memory recalls on spark was an out-of-band build, not a
> published artifact. The item's own acceptance criterion ("confirm 0.14.1 is
> released there … before shipping") therefore fails at step one. Unblock =
> cut a catpath release ≥0.14.1 first, then do the bump below.
>
> Also verified 2026-08-21, and it defuses the urgency: spark's
> `/opt/precis/venv` runs **0.13.0**, `constraints.txt` pins `==0.13.0`, and
> melchior carries **no** `PRECIS_AUTOCATPATH_VERSION` override. Engine, pin
> and idem-key token therefore agree, so the stale-engine dedup this item
> guards against is not currently live — the mismatch only appears when an
> unreleased engine is hand-installed over the pin. That is why the
> 2026-08-21 un-suspension did not need this bump.

## Motivation / why

The `PRECIS_AUTOCATPATH_VERSION=0.14.1` env override on melchior's worker
(and spark's out-of-band 0.14.1 engine install) is wiped by EVERY full
deploy — the worker plist re-renders from the template, which doesn't
carry it (auto-memory `qu164903-loop-fixes-followthrough`,
`catpath-dev-deploy`). Confirmed again 2026-08-16 post-deploy: the
override is gone from `com.precis.worker`. Without it the autocatpath
idem-key version token falls back to the pin-derived value
(`_autocatpath_pinned_version`, from the `>=0.13.0` floor in
`pyproject.toml`), so re-dispatches can dedup onto stale-engine jobs.
The design intent (comment at `src/precis/quest/compute.py` §engine
version token) is that the PIN is the one lever: every engine adoption
bumps it in the same commit.

## In scope

- `pyproject.toml`: `autocatpath>=0.13.0` → `>=0.14.1` in BOTH the
  `catalyst` extra and `catalyst-gpu` (`autocatpath[mace]`).
- Verify install channel: PyPI publish is deliberately disabled for
  catpath ("release-gated"); cluster venvs install from the GH release —
  confirm 0.14.1 is released there and the constraint resolves in the
  gate container before shipping.
- After ship: `scripts/deploy` (cluster venvs) AND
  `ansible-playbook 44-autocatpath.yml` (spark GPU engine venv — NOT in
  redeploy-precis.yml). Then remove the now-redundant env override
  recipe from memory.

## Explicitly NOT in scope

- Any engine code change; catpath releases from its own repo.

## Acceptance criteria

- Fresh gate run resolves autocatpath>=0.14.1 (no PyPI dependency).
- Post-deploy: worker venvs report autocatpath 0.14.1; idem-key token
  derives 0.14.1 with NO env override present.

## Target + blast radius

`pyproject.toml` extras; every venv serving the `pathway` kind; spark
GPU engine venv via 44-autocatpath.yml.

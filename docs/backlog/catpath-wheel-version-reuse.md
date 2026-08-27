---
status: draft
title: a hand-passed autocatpath wheel cannot be identified — catpath reuses one version across many commits
---

# catpath wheel version reuse

## Motivation / why

catpath keeps a single version number across many commits. `0.18.0` already
spans nine, including a minimum-image-convention fix in `validate`, per-step
trust records, and a post-convergence stability probe in `relax`. So two files
both named `autocatpath-0.18.0-py3-none-any.whl` can carry materially
different code.

Nothing downstream can tell them apart. `--find-links` matches on version, the
wheelhouse keeps whichever landed last, and neither the resolver, `uv pip
list`, nor the deploy log records which build is installed. A cluster can run
the pre-MIC-fix `0.18.0` while every version check reports it as current.

This is the residual of gr263082, whose primary defect (no wheel channel at
all to the `autocatpath_plugin` hosts) is fixed and verified in prod.

## What is already covered

`scripts/deploy`'s preflight closes the **locally-built** path exactly. It
refuses to build unless the catpath checkout is clean, and — since the
`uv.lock` change — unless the checkout would build the code the lockfile
pins, compared as `git diff <locked-sha> HEAD -- src pyproject.toml`. The
identifier was already there: `uv lock` records the resolved catpath commit,
and `uv lock -P autocatpath` moves it.

## What is still open

The **hand-passed** path: `-e autocatpath_wheel=<path>`, used when the wheel
is cut on the release machine rather than built locally. A wheel file carries
no provenance, so no consumer-side check can identify it. A stale wheel passed
this way is still undetectable.

## Options, and the one to prefer

- **Git-derived versioning in catpath** (`hatch-vcs` / `setuptools-scm` →
  `0.18.1.dev5+g4f059dc`). Preferred. Automatic, so it cannot be forgotten;
  names the exact commit rather than counting; keeps PEP 440 ordering, and a
  local version segment still satisfies a `>=0.18.0` floor.
- Record the built sha in the wheelhouse and assert it during deploy. Works,
  but needs catpath to emit the sha somewhere a consumer can read.

**Explicitly declined (Reto asked, 2026-08-27): bumping catpath's patch
version on every commit.** It would work, but it is a discipline fix for a
discipline failure — the original collision happened because a human skipped
version bookkeeping, and a rule demanding bookkeeping on *every* commit fails
the same way, just less often, and is unenforceable without a CI check that
the version moved. It also degrades the number's meaning, since patch
conventionally signals a bug fix and docs-only commits would consume it.

## Priority

Low. The common path is covered, and releases are infrequent. This matters
only when someone hand-passes a wheel built on the release machine.

## Target + blast radius

The catpath repo (build backend / version config), not this one. Consumer
side would be `scripts/deploy` + `scripts/lib/autocatpath-wheel.sh` if the
sha-assert option is taken instead. No schema change.

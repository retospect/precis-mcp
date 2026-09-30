---
status: ready
title: a policy gate that crashes mid-scan must not look like a policy violation
prio: high
pillar: platform
---

# Policy gates must distinguish "scan aborted" from "scan found something"

`tests/test_deploy_tree_no_secrets.py::test_repo_carries_no_cluster_addresses`
walks the tree inside the assertion. On 2026-09-29 it died partway through
with:

```
OSError: [Errno 23] Too many open files in system:
  '/app/src/precis/utils/__pycache__/claude_p.cpython-313.pyc'
```

raised from `_scannable_files` → `path.is_file()`. The scan never reached a
verdict. But the failure renders as `FAILED
test_repo_carries_no_cluster_addresses`, which is the *same red* a real
tailnet address in the tree produces — and the obvious reading of that name
is "there is a secret in the repo".

This session read it that way for several minutes and told a peer session the
wrong thing. The next reader will not necessarily catch it.

## Why this class specifically

Errno 23 is `ENFILE` — the **system-wide** file-table limit, not per-process
`EMFILE`. On a shared Docker VM with many concurrent sessions it is caused by
whatever else is running, and no `ulimit` change inside the failing session
helps. So it is not rare, not local, and not fixable by the person who hits
it. (Signature + recovery: `memory/gate-errno23-fd-exhaustion.md`.)

It is most dangerous on **policy** gates — the secret scanner, the ratchets,
the epsilon checks — precisely because those are the tests where a red is
supposed to mean "your diff violated a rule". For an ordinary unit test a
crash and a failure are both "something is wrong with the code"; for a policy
gate they are opposite conclusions, and only one of them is about your diff.

## In scope

Make an aborted scan structurally distinguishable from a violation, in the
tree-walking policy tests:

- Catch `OSError` in the walk and re-raise as something that names it — an
  explicit "scan did not complete, this is not a verdict" failure, or an
  `xfail`/error distinct from the assertion failure.
- Never let a partial scan reach `assert not hits`: a scan that aborted early
  has a *meaningless* empty-or-partial hit list, so a "pass" would be just as
  wrong as the misread failure — arguably worse, since a real secret could
  slip through a crash that happened to occur after the assertion's sample.
- The message should say the scan aborted, name the errno, and say "re-run in
  isolation; this is not a finding about your diff".

Apply to every tree-walking policy test, not just the secret scanner — this is
a shared shape (`_scan`/`_scannable_files` and its siblings), so fix it at the
walker, not per test.

## Explicitly NOT in scope

- Reducing fd usage on the VM, or chasing the concurrency that causes ENFILE.
  The point here is that the gate must report honestly when it happens, not
  that it must never happen.
- Retry-on-OSError inside the test. A gate that silently retries is a gate
  whose timing you no longer understand; make it fail legibly instead.

## Acceptance criteria

- A test that injects an `OSError` into the walk fails with a message naming
  an incomplete scan, and that failure is not phrased as a policy violation.
- The same injection never produces a pass.
- A real forbidden pattern still fails with the existing violation message,
  unchanged.

---
status: idea
title: the gate container has no SYS_PTRACE, so a slow-vs-wedged gate can't be told apart
---

# `py-spy dump` can't run inside the gate container

## What
When a local gate appears stuck, the one question that matters is whether
it is grinding or wedged. `py-spy dump` answers it in seconds — but inside
the gate container it fails with `Permission denied`, because the container
runs without `SYS_PTRACE`.

Split out of `go-gate-wall-clock-long-tail.md` (2026-09-25), where this cost
a 15-minute guess on a run that turned out to be merely slow. It is
independent of the marker work that closed that item.

## Why it matters
Three separate hang signatures already exist for the gate and they need
different responses:

- genuinely slow (the `slow` compute cluster — now deselected by default)
- the two 95–98% stall signatures in auto-memory `gate-lane-docs-only-trap`
- OOM-137 / bind-mount / shared-test-DB flakes (auto-memory
  `gate-oom-silent-death`, `gate-pycache-collection-flake`)

Without a stack dump the operator distinguishes them by waiting, which is
the most expensive possible probe — and the guidance in `/go` step 6
("classify before you re-run") is hard to follow with no classifier.

## Shape of the fix
Add `--cap-add=SYS_PTRACE` to the gate container invocation in
`scripts/test` (and whatever `scripts/ship` uses for the gate run). It is a
dev-container-only capability; nothing in the deployed fleet is affected.

Worth checking whether the Docker-for-Mac / colima seccomp profile also
needs relaxing before `py-spy` works, rather than assuming the cap alone is
enough.

## Not in scope
The hang signatures themselves — they are recorded in auto-memory and are
diagnosed, not open questions.

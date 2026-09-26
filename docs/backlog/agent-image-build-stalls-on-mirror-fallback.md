---
status: idea
title: the agent image build takes an hour-plus whenever the pre-pull falls back to mirror.gcr.io
---

# `docker build --target agent` stalls for an hour on the mirror path

## What
Three deploys on 2026-09-26, same host (melchior), same playbook task
(`deploy/playbooks/33-precis-agent-image.yml`):

| deploy | sha | agent build |
| --- | --- | --- |
| 06:14 | eee5edf4 | 3684s (61 min) |
| 07:22 | 27f5fc3f | **92s** |
| 08:35 | 2447ab97 | 7329s (2h02m, across a watchdog kill + retry) |

All three ran with `BUILDKIT_SYNTAX=mirror.gcr.io/docker/dockerfile:1.7`
and `PYTHON_IMAGE=mirror.gcr.io/library/python:3.12-slim-bookworm`, i.e.
the gr307314 pre-pull fallback was active every time. So the mirror alone
is not the discriminator — the 92s build used it too.

The 08:35 run hit the 3600s async watchdog, was killed, retried after 30s,
and the retry **succeeded**. Total task time 2h02m for a step that takes
92s on a good day.

## What the host can and cannot see
During both slow builds, sampled repeatedly from the host:

- deploy colima VM (`Virtualization.framework`, pid 469): 0–1% CPU
- VM user-mode network stack (`limactl usernet`): ~0.5s CPU per 20 min
- disk: tens of ops per 3s window
- the `docker build` / `docker-buildx` processes: alive, ~0.2s CPU total

Flat on all three signals, for tens of minutes, on a build that then
completed normally. **Host-side idleness says nothing about whether this
step is progressing** — that was mis-read twice in one session. Only the
watchdog and the task's exit code are load-bearing.

Note also that ansible's stdout is block-buffered through `tee`, so the
`ASYNC POLL` lines (poll: 15) do not reach the live log during a single
long task. `tail -F` on the deploy log is blind here; `ps` is the liveness
check.

## Suspected cause
The playbook's own comment at the `retries: 3 / delay: 30` setting says the
spacing was widened from `2/10` because "back-to-back retries hit the SAME
poisoned daemon-side DNS cache entry every time" (gr307314). A poisoned
daemon-side DNS entry inside the deploy user's colima VM matches the
symptom: the build sits waiting on a registry fetch that neither fails fast
nor progresses. Auto-memory `colima-dns-cache-poisoning` records that a
colima restart does **not** clear this and that the previous instance
needed d8570ec6.

Unverified — nobody has looked inside the VM's docker daemon while a build
is in this state. That is the next step, and it needs a shell as the
`deploy` user on melchior.

## Why it matters
Every deploy that invalidates a cached layer pays this. The 08:35 deploy
was an outage repair: prod web was restored at 08:38, but the deploy did
not exit until 10:09, holding the deploy lock for 90 further minutes and
blocking a queued sibling ship the whole time.

## Options
- Diagnose the daemon-side DNS state during a stall before changing
  anything (needs the `deploy` shell).
- Give the pre-pull a way to report *why* it fell back to the mirror; today
  the fallback is silent in the log, so "was the registry reachable" is not
  recoverable after the fact.
- Consider whether the 3600s watchdog is the right ceiling given a legit
  cold build measured 43 min and a stalled one is indistinguishable until
  it fires.

## Not in scope
The mirror fallback itself (gr307314) — it works, and it is what keeps the
build possible at all when the registry is unreachable.

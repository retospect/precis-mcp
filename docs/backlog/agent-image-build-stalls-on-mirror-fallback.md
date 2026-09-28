---
status: idea
title: the agent image build wedged on an apt/nodesource fetch and the watchdog kill ABORTED the deploy instead of retrying (2026-09-27); hour-plus cold-deps runs are a separate, benign cause
---

# The agent image build's hour-plus runs are probably not a stall

> **Retitled 2026-09-26.** The original framing — "stalls on the mirror
> fallback", suspected daemon-side DNS poisoning, needs a `deploy` shell
> during a stall — now looks wrong on all three counts. See "The cold-cache
> reading" below, which explains all three data points without a hang. The
> evidence for the original framing is kept because it is still the
> observation record, not because it is still the leading theory.

## What
Three deploys on 2026-09-26, same host, same playbook task
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

- deploy colima VM (`Virtualization.framework`): 0–1% CPU
- VM user-mode network stack (`limactl usernet`): ~0.5s CPU per 20 min
- disk: tens of ops per 3s window
- the `docker build` / `docker-buildx` processes: alive, ~0.2s CPU total

Flat on all three signals, for tens of minutes, on a build that then
completed normally. **Host-side idleness says nothing about whether this
step is progressing** — that was mis-read twice in one session. Only the
watchdog and the task's exit code are load-bearing.

## The cold-cache reading (2026-09-26, now the leading one)
`docker/Dockerfile`'s `deps` stage runs `apt-get install build-essential
libpq-dev git`, `pip install uv`, then `uv sync --frozen --no-install-project
--all-extras` — every extra, so torch comes with it, plus a private GitHub
clone for autocatpath. An hour-plus is what that **costs**. The stage is
invalidated by exactly two files, `pyproject.toml` and `uv.lock`.

| deploy | build | deps layer |
| --- | --- | --- |
| 06:14 eee5edf4 | 3684s | cold |
| 07:22 27f5fc3f | **92s** | warm — reused 06:14's cache |
| 08:35 2447ab97 | 7329s | cold — **confirmed** |

`2447ab97` is the only one of the three carrying a `pyproject.toml` change:
443d8295, the `src/precis_surface` wheel repair, committed 08:21, deployed
08:35. Verified with `git merge-base --is-ancestor 443d8295 <sha>` — true for
2447ab97, false for 27f5fc3f.

For 06:14 the likely trigger is the mirror fallback itself, but **not** as a
hang: when it fires, the build gets `--build-arg
PYTHON_IMAGE=mirror.gcr.io/library/python:…`. The digest pin keeps the
content byte-identical, but the **cache key is not** — a different `FROM` ref
discards the entire `deps` stage beneath it. So the key flip-flops with
Docker Hub's health, and every flip buys a full cold rebuild. Unconfirmed,
because the fallback was silent until this change.

### The fix this implies
1. **Stop the cache key flip-flopping** — build from one registry ref
   unconditionally instead of switching on Hub's health at build time. The
   mirror is the sensible fixed choice (proven, and it is what keeps builds
   possible when Hub is down). Deliberately NOT done yet: it is a behaviour
   change to the registry path premised on the unconfirmed half of the above,
   and the instrumentation below settles it on the next deploy for free.
   Measure, then change.
2. **Take the build off the deploy critical path** — rebuild only when
   `pyproject.toml`/`uv.lock` move, push to a registry, have deploys pull.
   Then even a legitimate two-hour cold build never holds the deploy lock.
   Tracked in `coalescing-deployer-for-many-sessions-one-fleet.md`'s
   neighbourhood.

Note in passing: both heavy steps use `--mount=type=cache` (uv's download
cache, apt's), so a watchdog kill re-downloads nothing. That is what makes
the silence ceiling below safe to set aggressively.

## Original suspected cause (kept for the record; see the retitle note)
The playbook's own comment at the `retries: 3 / delay: 30` setting says the
spacing was widened from `2/10` because "back-to-back retries hit the SAME
poisoned daemon-side DNS cache entry every time" (gr307314). A poisoned
daemon-side DNS entry inside the deploy user's colima VM matches the
symptom: the build sits waiting on a registry fetch that neither fails fast
nor progresses. Auto-memory `colima-dns-cache-poisoning` records that a
colima restart does **not** clear this and that the previous instance
needed d8570ec6.

Still unverified. Nobody has looked inside the VM's docker daemon while a
build is in this state.

## What shipped 2026-09-26 (instrumentation, not a fix)
The blocker used to be stated as "needs a `deploy`-user shell on the host
during a stall". That is a bad plan: it requires a human to be present
during an event nobody can predict, and all three observed incidents ended
**green**, so the playbook's failure-gated diagnostics never fired and
captured nothing. Inverted — the next stall now records itself:

- **`deploy/playbooks/files/precis-agent-build.sh`** wraps the build.
  `--progress=plain` makes BuildKit narrate each step into a host-side log
  at `<build-base-dir>/build-<sha>.log`, which survives an async kill (a
  killed `command` task has no stdout at all). A **progress** watchdog kills
  a build that has written nothing for `precis_agent_build_stall_sec`
  (default 900) and exits 124. Silence is the discriminator the flat
  total-elapsed ceiling never had: a legitimate cold build measured 43 min
  but never goes quiet for 15.
- **Timing + the last 25 progress lines are reported on every build**, not
  only failures — so a slow success, which is the actual symptom here, stops
  being invisible.
- **The mirror fallback is no longer silent.** The pre-pull always stamped
  `PULL_SOURCE`, but nothing printed it; "was the canonical registry
  reachable during that deploy?" was unanswerable after the fact. Now
  reported every run, with the canonical failure text when it fell back.
- **`PYTHONUNBUFFERED=1` on `scripts/deploy`'s ansible invocations.** Python
  block-buffers stdout into a pipe, which is why `tail -F` on a deploy log
  was blind during a single long task and `ps` was the only liveness check.
  The `ASYNC POLL` lines now arrive live.

## What is still open
Which reading is right. The next deploy decides it at no cost, because the
timing report now prints on every build:

- **cold-cache reading** — the log shows continuous progress through
  `apt-get` / `uv sync`, no `WATCHDOG` line, and the elapsed tracks whether
  `pyproject.toml`/`uv.lock` moved. Then fix #1 above and stop calling it a
  stall.
- **genuine wedge** — the watchdog fires and the last line before it names a
  step that produced nothing. Then the DNS theory is back on the table, with
  the stuck step finally identified.

Two things to weigh once there is a second data point:

- Whether the 900s stall ceiling is right. Too tight costs one retry off a
  ladder that has three (and re-downloads nothing, per the cache mounts);
  too loose and we are back to hour-long deploys.
- Whether `precis_agent_build_timeout_sec` (3600) is even the right shape. If
  cold builds legitimately take 2h, a total-elapsed ceiling below that is
  actively harmful and silence is the only ceiling worth having.

## Not in scope
The mirror fallback itself (gr307314) — it works, and it is what keeps the
build possible at all when the registry is unreachable.

## Second data point, 2026-09-27 — the watchdog fired, and the abort is a new defect

The deploy of `6008588c` (18:39–19:03Z,
`.deploy-logs/20260927-183925-6008588c4851….log`) is the second data point
this item was waiting on. It lands on the **genuine wedge** branch, not the
cold-cache one, and it exposes a second problem the instrumentation did not
anticipate.

What the controller log shows, verified here: the last task is `docker build
--target agent` (`:524`), inside the final play "(Re)build the precis-agent
image where the container executor lane runs". ~14 `ASYNC POLL` lines at
`started=True finished=False`, then:

    [ERROR]: Task failed: [Errno 2] No such file or directory
    Origin: .../33-precis-agent-image.yml:524:11
    fatal: [melchior]: FAILED! => {"changed": false, "msg": "Task failed: [Errno 2] No such file or directory"}
    melchior : ok=185 changed=25 unreachable=0 failed=1

Reported from melchior's host-side trail by the session that ran it (not yet
verified here — the trail has not been captured into this repo): the build
went quiet **908 s into the `apt-get` step (#22, Debian/nodesource fetch)**,
the 900 s progress watchdog killed it, **rc 130**.

### Three findings

1. **The stall is real and the stuck step is now named.** 900 s of silence in
   an `apt-get` fetch is not a slow cold build — so the silence ceiling did
   exactly the job it was built for, and the cold-cache reading does not
   explain this one. The wedged fetch is `apt`/nodesource, i.e. name
   resolution or egress inside the deploy user's colima VM, which puts the
   DNS reading back on the table with a concrete step attached.

2. **The retries ladder did not engage — this aborts the deploy.** The task
   carries `retries: 3`, `delay: 30`, `until: (agent_build.rc | default(1))
   == 0` and `failed_when: false`, and the comments assert a watchdog kill is
   "a non-zero rc like any other, so the retries/until ladder picks it up
   unchanged". Observed: one attempt, a `FileNotFoundError` raised as a task
   ERROR, play aborted. A task-level exception short-circuits `until` and
   `failed_when` both, so gr335099's whole point — that a killed build costs
   one retry, not the deploy — did not hold. A candidate mechanism worth
   checking first: `_agent_build_wrapper` resolves to
   `{{ _agent_build_dir }}/deploy/playbooks/files/precis-agent-build.sh`,
   inside the **sha-scoped** build dir that the cleanup task wipes per-sha; if
   that dir goes away between the kill and the retry, the retry's own exec
   raises exactly `[Errno 2]`.

3. **The trail did not reach the controller.** `failed_when: false` exists so
   the diagnostics and the "Read the build's progress trail from the host"
   slurp still run on a build that never converged. A raised exception skips
   them: grepping the controller log for `WATCHDOG`, `rc=130`, `apt-get`,
   `#22`, `nodesource` returns nothing. The 09-26 design — "the next stall
   records itself" — held on the host and failed at delivery, so reading it
   still took a manual ssh. Fixing (2) fixes this as a side effect; if (2)
   turns out to be hard, the slurp wants to be in a `block`/`always` instead.

### Blast radius, for the record

The failing play is the last one, after "Bounce all precis daemons" and the
asa refresh, and melchior's recap is `ok=185 changed=25 failed=1` — so every
venv install and daemon bounce completed on all six hosts. Venvs were uniform
at `6008588c`; what stayed stale is melchior's resident `precis-agent` image,
still labelled `d9a7f115`. "Mixed fleet" overstates it. The consequence that
does bite: with `PRECIS_AGENT_CONTAINER=1` on the agent lane, container-
executed agentic jobs on melchior keep running the old image, so a code fix
shipped to the venvs is not live for them until an image rebuild succeeds.

## Two clean rebuilds since, 2026-09-28

The next two deploys both rebuilt the image without incident on melchior:

    agent image build rc=0, elapsed 184s, pull source mirror.gcr.io
    precis-agent image rebuilt

184 s against the 908 s that tripped the watchdog, same host, same
`mirror.gcr.io` pull source, one day later. So the apt/nodesource wedge was
transient egress, not a structural defect in the play — the image is current
again and the blast-radius note above is closed.

What this does **not** settle is findings (2) and (3), which are about the
play's error handling rather than the stall: a task-level exception still
short-circuits the `retries`/`until` ladder, and the progress-trail slurp
still sits in the same block as the raising task, so the next stall — from
any cause — will again abort the deploy and deliver no trail. Those remain
open and are the reason this item is still here.

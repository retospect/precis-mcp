# Deploy: the agent-image build task died on the controller with `[Errno 2]` mid-poll, before the host watchdog fired — retries ladder and diagnostics never ran

`scripts/deploy 6008588c4851fd6063d62815e580eabb79222269 --pinned`, 2026-09-27
18:39–19:03 UTC, persisted at
`.deploy-logs/20260927-183925-6008588c4851fd6063d62815e580eabb79222269.log`.
Every rollout play before the last one completed on all five nodes (venvs
uniform at 6008588c). The last play, `deploy/playbooks/33-precis-agent-image.yml`,
failed on melchior in "docker build --target agent (BuildKit; label stamps the
built sha)" (`async`, `poll: 15`, `retries: 3`, `until rc == 0`,
`failed_when: false`), so the resident `precis-agent` image stayed labelled
`d9a7f115`. Narrow consequence: the agent container on melchior runs the
previous sha; nothing else is stale.

## Timeline (two independent failures, controller first)

| UTC | where | event |
|---|---|---|
| 18:46:34 | controller | build task starts (async), 67 `ASYNC POLL` lines follow (15 s each ≈ 16.75 min) |
| ~18:48:52 | melchior | last BuildKit output: step #22 `apt-get` InRelease fetches from deb.debian.org / deb.nodesource.com |
| 19:03:03 | controller | 68th poll raises `Task failed: [Errno 2] No such file or directory`, `Origin: …/.git/precis-deploy-tree/deploy/playbooks/33-precis-agent-image.yml:524:11 (source not shown: FileNotFoundError)`; play aborts, melchior `ok=185 changed=25 failed=1` |
| 19:04:21 | melchior | wrapper watchdog: no build output for 908 s (ceiling 900), kills the build; `#22 CANCELED`, `done rc=130 elapsed=1067s` |

The controller died **78 s before** the host killed the build, so the retries
ladder was never reached for a reason unrelated to the stall. Ansible could not
even show the task source at 19:03:03: the detached render worktree
`.git/precis-deploy-tree` (which `scripts/deploy` creates under the deploy lock
and removes only in its EXIT trap) was already gone while ansible was still
running. Something outside this deploy removed it mid-run; the concurrent gate run
(`scripts/ship --mutate --full`, 18:54–19:07) has no code path to it. The
fitting mechanism — `scripts/inflight` advertising the live render tree as
removable — is filed in `inflight-lists-the-live-deploy-render-tree-as-removable.md`.

Host-side trail, verbatim tail of
`~/.cache/precis-agent-build/build-6008588c4851fd6063d62815e580eabb79222269.log`
on melchior (the slurp task "Read the build's progress trail from the host" never
ran, so none of this reached the controller log):

```
#20 DONE 111.1s
#21 [agent 2/5] COPY docker/agent-mcp.json /etc/precis/agent-mcp.json
#21 DONE 0.0s
#22 [agent 3/5] RUN --mount=type=cache,target=/var/cache/apt,sharing=locked     --mount=type=cache,target=/var/lib/ap...
#22 0.116 Get:1 http://deb.debian.org/debian bookworm InRelease [151 kB]
#22 3.299 Get:2 https://deb.nodesource.com/node_20.x nodistro InRelease [12.1 kB]
#22 3.344 Get:3 https://deb.nodesource.com/node_20.x nodistro/main arm64 Packages [14.4 kB]
#22 30.15 Ign:1 http://deb.debian.org/debian bookworm InRelease
#22 30.17 Get:4 http://deb.debian.org/debian bookworm-updates InRelease [55.4 kB]
#22 30.19 Get:5 http://deb.debian.org/debian-security bookworm-security InRelease [34.4 kB]
precis-agent-build: WATCHDOG — no build output for 908s (ceiling 900s), killing pid 71370. The last step above is whe...
#22 CANCELED
ERROR: failed to build: failed to solve: Canceled: context canceled
precis-agent-build: done rc=130 elapsed=1067s
```

`rc=130` in that line is the docker child's exit code; the wrapper
(`deploy/playbooks/files/precis-agent-build.sh`) then exits 124 when the stall
flag is set, as the playbook comments say. The controller never saw either
value. The sha-scoped build dir and the wrapper inside it are still present on
melchior (wrapper mtime = archive time), so "the retry re-exec'd a wiped
wrapper" is ruled out.

## To do

1. **Controller:** the removal mechanism and its fix live in
   `inflight-lists-the-live-deploy-render-tree-as-removable.md` (deployer
   tree): `scripts/inflight` buckets the live render tree as "Removable
   (merged + clean + no live session)" and prints the removal command for
   it without checking the deploy lock, and the dead-holder lock steal in
   `scripts/deploy` decides liveness with `kill -0`, which is EPERM across
   users. This item adds only: decide whether a play file missing mid-run
   should abort a *running* play at all (ansible re-reads it only to render
   the error).
2. **Host:** the `apt-get` stall in step #22 (Debian + nodesource InRelease,
   then 900 s of silence) is a second occurrence class next to
   `agent-image-build-stalls-on-mirror-fallback.md` (registry side). Consider
   an apt mirror fallback or a pre-flight reachability check for
   deb.debian.org / deb.nodesource.com the way gr307314 did for the registry.
3. **Instrumentation gap:** the 2026-09-26 trail slurp is inside the same block
   as the build task; when the *task* raises (as opposed to failing), the
   slurp never runs. Move the trail read to an `always:` so a host-side stall
   record always reaches the controller log.

Owner: `scripts/deploy`, `deploy/playbooks/33-precis-agent-image.yml`.
Repro: any pinned deploy whose agent-image build outlasts a concurrent worktree
operation on the controller; the apt stall reproduces by itself when the
mirrors are slow.

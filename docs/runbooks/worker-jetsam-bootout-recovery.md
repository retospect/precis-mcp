# Worker de-domained from launchd (jetsam cull / crash-loop bootout)

**Symptom.** A melchior precis daemon is simply absent: `sudo launchctl
kickstart -k system/com.precis.worker` →
`Could not find service "com.precis.worker" in domain for system`. No process,
no revival. (`launchctl list | grep precis` looks empty as a non-root ssh user
— system-domain daemons need `sudo`; that is not proof they are unloaded.)

**Cause.** macOS **jetsam** culls low-band precis LaunchDaemons on **melchior**
under RAM pressure — it co-hosts the llama.cpp servers wiring ~70 GB+, and a
large Marker ingest on top tips it over (`launchctl print` → `immediate reason
= inefficient`). When a worker then crash-loops, launchd hits `ThrottleInterval`
and can **boot it out of the system domain entirely**: `KeepAlive` cannot
revive an unregistered job, so it stays dead until a manual reload. `kickstart`
can't help — the service is *unloaded*, not stopped.

## Recovery

1. Check state: `ssh melchior 'sudo launchctl print system/com.precis.worker'`
   (empty = de-domained).
2. Re-register:

       ssh melchior 'sudo launchctl bootstrap system /Library/LaunchDaemons/com.precis.worker.plist'

   A bootstrap right after a bootout often fails `EIO 5` (label not yet
   released) — retry after a short sleep.
3. **Without raw sudo** (the permission classifier blocks `ssh melchior sudo
   launchctl bootstrap`): re-run the **dedicated** playbooks, which carry the
   verified reload handler, from the repo `deploy/` tree (`ansible.cfg` has
   key/vault/become):

       ansible-playbook playbooks/20-precis-worker.yml --limit melchior          # system worker
       ansible-playbook playbooks/37-precis-worker-agent.yml --limit melchior    # agent worker

## Don't assume a redeploy fixed it

The `precis_worker` role's reload handler does bootout → bootstrap → kickstart
with retries and a PID assert. But a `scripts/deploy` (`redeploy-precis.yml`)
bounce once sent SIGTERM to **both** melchior workers and left both
un-bootstrapped, silently (`failed=0`) — so the redeploy bounce path is not
reliable for this. gr162132: make the redeploy bounce reuse the pid-verified
handler / assert a live pid post-bounce. After any deploy that touched
melchior, confirm a live pid.

## Hardening already in place

`ProcessType=Interactive` in the plist raises the jetsam band toward the
foreground tier so the worker is culled last. Applied to the agent worker, the
system worker (runs `fetch_oa`, pinned to melchior, plus
embed/summarize/dispatch) and asa-bot.

## Consequences of a melchior cull

- **`dispatch` effectively runs ONLY on melchior** (7-day `worker_logs`: the
  other nodes last ran it 2026-07-08Z), so a melchior cull freezes planner
  `plan_tick` minting **cluster-wide** with no failover — even though dispatch
  is in every system node's profile and is `SKIP LOCKED`-safe. The outage is
  silent. gr55748 (PRIO:high): investigate why only melchior runs it, and add a
  nursery "no plan_tick in N min while open LLM todos exist" alert.
- On recovery the worker once **starved `dispatch` behind a slow `fetch_oa`
  backlog** (~25 min/cycle) because the sequential ref-pass loop ran fetch
  before dispatch. Fixed: `src/precis/cli/worker.py::_REF_PASS_PRIORITY` sorts
  `ref_passes` so job-exec and planner lifecycle
  (dispatch / auto_check / schedule / sweeper / job_claude_inproc) run ahead of
  background fetch/enrichment/reviewers each cycle (stable sort, both profiles).

asa-bot has the same signature: [`asa-bot-oauth-and-deploy`](./asa-bot-oauth-and-deploy.md).
Stalled-job triage: [`stalled-claude-inproc-jobs`](./stalled-claude-inproc-jobs.md).
Nightly DB-node reboot: [`prod-db-down-after-reboot`](./prod-db-down-after-reboot.md).

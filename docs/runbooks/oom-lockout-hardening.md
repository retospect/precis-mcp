# spark OOM lockout hardening (why we can't be locked out by a RAM-thrash)

> spark is a single shared compute box (GB10). It runs the precis worker
> (in-process MACE/NEB autocatpath compute), llama-swap, the bge-m3 embedder, and
> PDF ingest (marker) all at once. When memory runs out the box thrashes into
> swap and **sshd stops answering banner-exchange while the host still pings** —
> and there is no remote power tool, so recovery meant a physical reboot.
> Observed 2026-07-19, -20, -24. This runbook is the durable fix + how to
> deploy and verify it. Config lives in `deploy/roles/hardening/`.

## The two layers

1. **sshd is un-killable by the kernel OOM killer** — a systemd drop-in
   `/etc/systemd/system/ssh.service.d/oom.conf` sets `OOMScoreAdjust=-1000`, so
   the kernel OOM killer never selects sshd (or its forked login sessions). Even
   in a worst-case true-exhaustion event, the kernel reaps the memory hog and
   leaves the door open. This is the load-bearing guarantee — always applied on
   Linux nodes.

2. **earlyoom kills the hog early** — before the box thrashes far enough to lock
   us out. `-m 10,5 -s 10,5`: SIGTERM the biggest `--prefer` match when free
   memory (or swap) drops below 10%, SIGKILL below 5%. `--avoid` protects
   `sshd|systemd|tailscaled|autossh|cron` (never killed → the box stays
   reachable); `--prefer` biases the kill toward the compute hogs
   (`python|precis|llama-server|llama-swap|ollama|marker`). The precis worker
   restarts via `Restart=always`, so culling it is the recoverable sacrifice;
   an interrupted autocatpath NEB is re-driven by the ssh_node crash-recovery
   (lease-steal + poison-cap).

3. *(optional, off by default)* a hard `MemoryMax` cap on
   `precis-worker.service` via
   `/etc/systemd/system/precis-worker.service.d/memory.conf`. Enable by setting
   `linux_compute_memory_max` (e.g. `"96G"`). Left off until a value is measured
   against a real NEB working set — an over-tight cap OOM-kills legitimate runs.

## Deploy

The `hardening` role is **not** in `redeploy-precis.yml` (what `scripts/deploy`
runs), so a normal precis redeploy will not apply this. Run the hardening
playbook directly, scoped to the Linux node(s):

    cd deploy
    ansible-playbook -i <inventory> playbooks/10-hardening.yml --limit spark
    # or --limit linux (the os_family == "linux" group)

Idempotent: re-running is a no-op once converged. The sshd drop-in triggers a
`daemon-reload` + `ssh.service` restart (live sessions survive on Debian/Ubuntu).

## Verify (once spark answers ssh)

    # sshd is OOM-immune: the running sshd's oom_score_adj is -1000
    ssh spark 'cat /proc/$(systemctl show -p MainPID --value ssh)/oom_score_adj'   # → -1000
    ssh spark 'systemctl show ssh -p OOMScoreAdjust'                                # → OOMScoreAdjust=-1000

    # earlyoom is running with our args
    ssh spark 'systemctl is-active earlyoom && systemctl cat earlyoom | grep EnvironmentFile'
    ssh spark 'cat /etc/default/earlyoom'
    ssh spark 'journalctl -u earlyoom -n 20 --no-pager'   # startup line echoes the thresholds + regexes

    # (if the cap was enabled) the compute worker's MemoryMax
    ssh spark 'systemctl show precis-worker -p MemoryMax'

## Notes

- earlyoom `--avoid`/`--prefer` regexes match the process **comm** (truncated to
  15 chars), anchored `^...$`. If a compute process's comm changes, update
  `earlyoom_args` in `deploy/roles/hardening/defaults/main.yml`.
- This does not replace the nightly gentle reboot; it prevents the *unplanned*
  thrash lockout between reboots.

## Per-unit memory guards + memsnap

Earlyoom does not catch file-backed mmap thrash (MemAvailable stays high). Use
cgroup limits: `precis_worker_memory_*`, `host_memory_guard_units`, `memsnap_enabled`
(roles/hardening/defaults/main.yml; example in
`deploy/inventory.example/group_vars/all/memory-guard.example.yml`). Apply with
`playbooks/10-hardening.yml`; daemon-reload only, no restarts. Read snapshots with
`journalctl -t memsnap`.

## Per-host memory budget (GB10 lanes)

Since 2026-10-15 (castor wedged twice: vLLM 0.70 + 56G compute + 24G sandbox +
24G worker on 121 GiB) each worker lane's cap comes from the inventory group
that places it (`precis_lane_memory` in `roles/hardening/defaults/main.yml`):
heartbeat `precis-worker` 4G on `serving`/`compute`, `precis-worker-compute`
48G + a 40G GPU-container reserve on `compute`, `precis-worker-sandbox` 2G + an
8G container reserve on `agent_sandbox_hosts`. One template
(`templates/memory-budget.json.j2`) writes the drop-ins and sums the host:
placed lane caps + container reserves + `host_memory_guard_units` + the LLM
share (`vllm_gpu_memory_utilization` / `sglang_mem_fraction_static` x
`host_memory_total`) + `host_memory_os_reserve` (10G) +
`host_memory_extra_reserve` (per host, non-precis services). Over
`host_memory_total` (121G), an LLM group joined with `compute`, or a placed
lane with no MemoryMax fails 10-hardening and the lane plays 35/43/49/49a.
`tests/test_deploy_memory_budget.py` checks every group combination from the
defaults. Per-unit exceptions go in `precis_worker_memory_overrides`, which the
budget counts too. To retire lanes from a host that stays on duty:
`playbooks/retire-worker-lanes.yml -e retire_host=<node> -e retire_lanes=compute,sandbox`.

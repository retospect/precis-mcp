# spark OOM lockout hardening (why we can't be locked out by a RAM-thrash)

> spark is a single shared compute box (GB10). It runs the precis worker
> (in-process MACE/NEB catpath compute), llama-swap, the bge-m3 embedder, and
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
   an interrupted catpath NEB is re-driven by the ssh_node crash-recovery
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

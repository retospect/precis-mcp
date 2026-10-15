# Host ops (`scripts/host-ops/`)

**What.** The operator scripts cluster-desk-da ran out of `/tmp/gripe-fleet/`
during the 2026-10-15 NFS/OS-update/host-lanes work, ported into the repo
unchanged except paths and host parameterisation (Reto "go host-ops",
2026-10-15 ~19:40Z). Every script takes `--help`, runs `set -euo pipefail`,
and resolves host coordinates from the gitignored overlay
(`deploy/inventory/hosts.yml` in the main checkout — `scripts/lib/pgb-host.sh`,
`scripts/lib/node-host.sh`): `nfs_server`/`shared_mount` vars, per-host
`lan_ip`, and the `linux`/`vllm_serving` groups. ssh goes by inventory name
(`~/.ssh/config` already maps them). No address is ever a literal.

**Permission.** Invoke as `scripts/host-ops/<name>` — direct invocation is
what the allow rule will cover; `bash scripts/host-ops/...` is NOT covered.
Allow line pending Reto in `.claude/settings.json`, next to
`Bash(scripts/fleet-wave:*)`:

    "Bash(scripts/host-ops/:*)"

## The scripts

| script | what it does |
|--------|--------------|
| `os-update` | Rolling OS + firmware update for ONE GB10 host: precis `service reserve` (no new heavy claims, 6 h) → wait for running compute to drain → stale apt/dpkg lock check (refuses on a holder older than 1 h, prints the kill line for Reto) → `apt full-upgrade` → `fwupd` → reboot → wait for ssh → kernel/`nvidia-smi`/docker-GPU/unit checks → `service release`. Flags: `--host` (required), `--dry-run`, `--drain-timeout MIN` (240), `--force-drain`. On castor it waits for vLLM `/health` after reboot — skipped when the unit is disabled. The reserve CLI runs on `RUN_ON` (default pollux; castor when updating pollux) because the target host reboots mid-run. |
| `vllm-stop` | Stop + disable the `vllm` unit and its container on the vLLM host (default: the overlay's `vllm_serving` member; `--host` overrides), printing memory before/after. `--start` re-enables and waits for `/health` (model load ~14 min). Refuses when `sglang` is active. |
| `nfs-recover` | The CLOSE_WAIT wedge fix: restart `nfsd` on the `nfs_server` host (postgres untouched; never reboot it — it is the DB host), then `ss -K` any client socket still in FIN-WAIT-2 toward the server's `lan_ip`, on every `linux`-group client. Verifies the `shared_mount` stat and that a fresh `rpcinfo` probe leaves no CLOSE_WAIT. Stops without escalating if the nfsd restart fails. |
| `nfs-keepalive` | Interim wedge mitigation: a transient `systemd-run` timer statfs'ing `shared_mount` every 60 s on each `linux`-group client so the NFS transport never idles (gone at reboot). `--stop` removes it. |
| `pin-prod` | `git -C $PROD_CHECKOUT fetch` + `checkout --detach origin/prod` and print the result. `PROD_CHECKOUT` defaults to `~/precis-mcp-prod`. |
| `play` | The pinned-checkout ansible wrapper: refuses unless `PROD_CHECKOUT` is exactly `origin/prod` (after a fetch), clean, and contains the host-lanes commit `932364d0a` (`HOST_LANES_SHA` env overrides the gate sha). Then runs `ansible-playbook -i <overlay hosts.yml> --vault-password-file deploy/.vault-pass -e precis_{worker,web,embedder}_git_ref=<prod sha> "$@"` from `$PROD_CHECKOUT/deploy` — the SHA_ARGS pin is what keeps a playbook from installing `main` (the 2026-10-15 castor venv break came from a run out of the main checkout). |
| `lanes-overlay` | The one-shot overlay patch for host-lanes: edits `hosts.yml` groups (new `vllm_serving`, castor out of `compute`, spark into `compute` + `agent_sandbox_hosts`), `group_vars/all/topology.yml` capability lists, and `host_vars/{spark,castor}.yml`. Dry run prints the diff; `--apply` writes. Every anchor must match exactly once or it aborts without writing — a re-run after the overlay is already patched refuses cleanly. Commit in the overlay repo afterwards. `PRECIS_OVERLAY_DIR` overrides the overlay location. |

## OS-update run order

`spark` → `pollux` → `castor`, strictly one host at a time — the reserve CLI
runs on pollux (on castor when pollux itself is the target), and the next
host starts only after the previous host's post-reboot checks are all
`active`. spark first because it is the canary (idle, no LLM); castor last
because its reboot takes vLLM down ~15–25 min.

## Host-lanes phase C order

After a release containing `932364d0a` is prod and `pin-prod` has re-pinned
the checkout (source: cluster-desk's run order, 2026-10-15 ~17:10Z):

1. `lanes-overlay` — read the diff, `--apply`, commit in the overlay repo.
2. `play playbooks/10-hardening.yml --tags oom-guard --limit castor,pollux,spark` — installs earlyoom on pollux, rewrites the memory caps (castor worker 24G→4G, pollux heartbeat 4G + compute 48G).
3. `play` `03-precis-constraints.yml`, `19-pgpass.yml`, `20c-precis-heartbeat-serving.yml` — all `--limit spark` (provisions spark's venv + heartbeat).
4. `play playbooks/44-autocatpath.yml --limit pollux,spark` with `autocatpath_wheel` + `tblite_wheel` extras; rewrites `ROUTE_NODE=pollux,spark`.
5. `play` `43-aizynth.yml --limit spark`, `46-alphafold.yml --limit pollux`, `43-precis-worker-compute.yml --limit pollux,spark`, then `34-code-task-image.yml` + `35-precis-worker-sandbox.yml --limit spark`.
6. Gateway re-render: `38-precis-web.yml` + `37-precis-worker-agent.yml` (golden's deploy renders the same) — only after this do new jobs stop pinning to castor.
7. `service_config`: spark job_claude_docker 0→5, then castor 5→0 (never a window with zero sandbox hosts).
8. castor `service reserve`; wait until `pgrep -fc precis_pathway.runner` on castor prints 0.
9. `play playbooks/retire-worker-lanes.yml -e retire_host=castor -e retire_lanes=compute,sandbox`, then castor `service release`.
10. `play playbooks/49-vllm.yml --limit castor` — re-renders vllm.service at the 0.60 memory fraction (~15 min LLM outage).
11. `os-update --host pollux`, then `--host castor` — each only after the previous host's checks are all active. Steps 10 and 11's castor half share one announced LLM window (~40 min).

Note: the `service_config` steps (7–9) still run the SQL helper out of
`/tmp/gripe-fleet/host-lanes-service-config.sh` — it was not part of this
port.

## Verify after the lanes land

- `ssh <h> systemctl show precis-worker precis-worker-compute precis-worker-sandbox -p Id -p MemoryMax -p ActiveState`
  - castor: precis-worker 4G active; no compute or sandbox unit.
  - pollux: precis-worker 4G, precis-worker-compute 48G.
  - spark: those two plus precis-worker-sandbox 2G.
- `/status` shows a heartbeat from spark; the next autocatpath seed lands on
  pollux or spark.

---
status: idea
title: Cluster residuals — diagnosed 2026-09-08, unfixed
---

# Cluster residuals — diagnosed 2026-09-08, unfixed

Found while chasing a false "two nodes are dead" alarm. Each is diagnosed to a
cause and left unfixed; none is urgent. Operating context and the traps that
produced the false alarm are in
[`docs/runbooks/cluster-ssh-remote-access.md`](../runbooks/cluster-ssh-remote-access.md).

No literal addresses here — the repo is public and the leak gate rejects them
tree-wide. Real coordinates live in the gitignored `deploy/inventory/`.

## 1. `code-task` image build is OOM-killed on the GPU node

The deploy task *"Build code-task:&lt;sha&gt; (podman)"* died with `rc: -9`
(SIGKILL) after **39 minutes**. That is an OOM kill, not a build error, and it
is the only reason that deploy reported `failed=1`.

The host was **not** short of memory: it has ~121 GB total with ~115 GB
available when inspected shortly after. So this is build-level — container
memory limit, or build parallelism inside the image — not host exhaustion. A
reboot does not address it and was wrongly considered.

Until fixed, the sandbox lane's image is missing on that node, and every deploy
will keep failing there. `PRECIS_DEPLOY_SKIP_CATPATH_WHEEL` has no bearing on
this; it is a separate preflight.

## 2. One GPU node has load pinned at exactly 3.00

`uptime` reports 3.00 across the 1/5/15-minute averages with a single logged-in
user — the flat-integer signature of processes stuck in uninterruptible sleep,
not real CPU work. This matches the known NFS/lockd wedge (Linux clients hang
D-state on the shared mount while macOS clients are fine).

The root cause lives on the DB Mac's lockd, so **rebooting the GPU node masks it
and it returns**. Fix at the source.

## 3. That same node never establishes a direct tailnet path

`tailscale ping` returns via the relay on every attempt and reports *"direct
connection not established"*, while its sibling does punch through to a direct
path intermittently.

This costs **throughput, not latency** — the measured direct path was actually
*slower* than the relay, because latency here is dominated by physical distance
between the controller and the cluster. It matters when pushing models or
datasets to that node, and not otherwise. Diagnose as NAT traversal / UDP
reachability, not as a Tailscale fault.

## 4. No subnet route is advertised for the cluster LAN

The on-LAN Mac advertises no routes, so the cluster LAN is unreachable from any
remote tailnet device and LAN-only services (e.g. the llama.cpp endpoint) are
invisible. Worked around today with per-node `<node>-lan` ssh aliases that
`ProxyJump` through the on-LAN Mac.

The proper fix, plus why the workaround is only a workaround, is in the runbook.
It needs a **web admin-console approval**, so it cannot be fully scripted.

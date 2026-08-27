---
status: ready
title: put(kind='job') cannot pass meta.requires — GPU-slot token unreachable from MCP
prio: medium
---

# Expose `requires=` on the MCP job put

Found dispatching the qu164903 tick-zero replication batch (2026-08-27):
`put(kind='job', job_type='autocatpath_seed', …)` exposes no way to set
`meta.requires={'gpu': 1}`, the slot token the campaign's own dispatcher
sets so per-host GPU work serializes. Replicate jobs dispatched via MCP
therefore land concurrently on one GPU and kill each other
(`infra:child-killed`, "child exited without writing result.json" — 4 of 5
Ir replicates died this way while the lone survivor on the same host
succeeded).

Fix: accept a `requires` mapping on the job put (validated shape,
whitelisted keys — at least `gpu`), stored to `meta.requires` exactly as
the internal dispatcher does. Alternatively (or additionally) default
`requires={'gpu':1}` server-side for job types known to need it
(autocatpath_seed NEB tiers).

DoD: an MCP-dispatched autocatpath_seed serializes with campaign-dispatched
GPU jobs on the same host; test covers the passthrough + the whitelist
rejection of unknown keys.

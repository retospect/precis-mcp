---
status: ready
title: Serving programme follow-ups — ordered resume pointer for the agentic-reasoning instrumentation / serving work
prio: high
---

# Serving programme follow-ups

Resume pointer for the work that came out of the 2026-09-29 design session
(Reto + agent, big-model-manage worktree, now landed and reaped). This file
only carries the **order** and the **cross-item constraints**, which live in
no single item.

Landed: `911c88d9` (nine backlog items), `0ed8c933` (the load-test harness +
its finding), `644acf08` (the residual note); `911c88d9` and `0ed8c933`
passed a full 23,732-test suite inside tree `f8f884d1`. **Undeployed and
ungated on main** — owed a settle-up `/go`; prod is pinned to `b81bf3cc`.

**The finding that changes design** (`mcp-concurrency-load-test.md`
`## MEASURED 2026-09-29`): one `precis serve` process serves ~28 MCP calls/s
total, flat from N=1 to N=64 — concurrency buys nothing. Server CPU pins at
1.17 of 12 cores (GIL-bound); Postgres peaks at 4 of 100 connections, nowhere
near the wall the harness was built to catch. The anyio pool is not the
constraint and raising it cannot help. ~24 concurrent sessions therefore
wants 2-3 `precis serve` processes behind a balancer, not a tuning knob.
Re-run: `scripts/mcp-loadtest --ramp 1,8,32,64 --duration 15`.

## The order, and why it is an order

1. **Unhang spark's `/mnt/cluster` NFS mount** — `statvfs` times out at
   120 s. `eval-run-spine.md` items 4 (content-addressed blob store) and 9
   (frozen eval world) both depend on that share. Fix or unmount before
   anything is built on it.
2. **spark host prep** — stop the Xorg+gnome-shell session (idles the box at
   load ~1.4), install `uv`, remove the `/opt/precis` orphan. Inspected
   2026-09-29: idle, no precis units, `/etc/precis` gone, 2.8 TB free, driver
   580.159.03 / CUDA 13.0. Docker Hub egress is confirmed blocked (20 s
   timeout); ghcr.io works. **A bench role is not cluster duty** — do not add
   spark to any service group or capability list. **Unverified**: spark runs
   Postgres 16; confirm prod's major version and that `pgvector` is present
   there before planning the frozen world as a restore
   (`eval-run-spine.md`'s decisions log, "Check the Postgres major version
   before planning the restore").
3. **`py-spy` profile of `precis serve` at N=32** — names which work holds
   the GIL (`mcp-concurrency-load-test.md` `### Still owed`, "Which work
   holds the GIL"). Decides whether 2-3 processes is a workaround or the
   permanent topology. ~1 hour.
4. **Slice 0 on spark** — serve Nemotron 3 Super 120B-A12B NVFP4 and gpt-oss
   120B MXFP4 as control, ramp concurrency, find where aggregate tok/s
   plateaus (`vllm-per-node-serving.md` "Slice 0"). Cheaper than budgeted:
   `vllm/vllm-openai:v0.20.1` is already cached on the box and the nvidia
   docker runtime is already configured.
5. **Pin `scripts/mcp_loadtest/`** — no tests, outside mypy's `src tests`
   scope, inert on the cluster (standalone script, imported by nothing).
   Unit-test the pure functions — `pct()`, `tool_error()`,
   `CpuMeter.read()`, `verdict()` — the verdict thresholds are the drift risk
   (`mcp-concurrency-load-test.md` `### Still owed`, first bullet).
6. **The multi-process arm** — 2-3 `precis serve` processes behind a
   balancer, re-ramped, to confirm throughput actually scales with processes
   (`mcp-concurrency-load-test.md` `### Still owed`, last bullet).

Why it is an order: 1 gates `eval-run-spine.md` items 4 and 9; 2 gates item 4
(GPU-in-container, egress) and the frozen world (Postgres major); 3 decides
whether 6 is topology or workaround; 4 is the go/no-go for the whole
oversubscription design — a Mamba-hybrid ceiling at c=8 kills it
(`vllm-per-node-serving.md` "Slice 0").

## Traps

- The three `vllm-per-node-serving.md` "Known footguns" — point, don't copy.
- Docker Hub egress is blocked from spark; ghcr.io works.
- The bench role must not enter inventory — no service group, no capability
  list.
- Memory `mcp-throughput-gil-ceiling` — being transferred from another
  machine.

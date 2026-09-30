---
status: draft
title: What has to leave process memory before the shared MCP can run more than one worker
prio: low
---

# What has to leave process memory before the shared MCP can run more than one worker

## Motivation / why

The shared session server is pinned to a single uvicorn worker, and
`session-mcp-http-server.md` says why: "**never** `--workers >1`: the serve
ledger's `WeakKeyDictionary`, the pagination-cursor registry
(`_init_runtime`'s `long_lived = True`, gr267466) and the DB pool are all
per-process." That pin is correct today and should stay until this item
lands — a second worker would silently split sessions across two memories,
so a `more(cursor=…)` would find its cursor only half the time.

The pin is also the ceiling. One process is one GIL, one pool, and one
crash. As the fan-out that motivated the shared server grows, the next
scaling step is either more workers behind the same port or a second
replica, and both need the same thing first: the per-session state has to
stop living in a Python dict keyed on a local object.

Filing this as the shape of the arc, not as work to start. The honest
position is that one worker has not yet been shown to be the bottleneck —
12 concurrent searches finished in 4.78 s wall against a 2.06 s single-call
baseline, so there is headroom. This item exists so the pin's reason is
written down somewhere other than a warning comment, and so the first
person who hits the ceiling does not rediscover the list.

## In scope

An inventory, with a decision per entry, of what is per-process today:

- **Serve ledger** (`serve_ledger.py`) — `WeakKeyDictionary` keyed on the
  MCP session object. Loss degrades to full serves, so this is the
  cheapest to externalise or simply accept as per-worker.
- **Pagination cursor registry** (`_pagination.py`, gr267466) — the one
  that genuinely breaks: a cursor minted on worker A is unreadable on
  worker B, and the failure is a user-visible "no such cursor".
- **Exit breadcrumb** (`install_watchdog.py`) — already per-session but
  held in a module-level value; N workers means N copies and N reads of a
  file that deletes on first read, so N-1 workers would report nothing.
- **DB pool** — N workers means N × `max_size`; the pgbouncer arithmetic
  in `session-mcp-http-server.md` assumes one.
- **Tool-concurrency semaphore** — process-wide today, so the cap becomes
  N × the configured value.

## Explicitly NOT in scope

- Doing it. This item is the inventory and the decision per entry; each
  externalisation that survives the decision gets its own item.
- Unwinding the shared transport, or moving the spawned callers (agent
  containers, sandbox sidecar, `asa_bot`) off stdio — see
  `mcp-shared-transport-concurrency.md`, which owns that boundary.
- Cross-*machine* sharing. Everything here is one host.

## Acceptance criteria

- Each entry above has a recorded decision: externalise, make per-worker
  deliberately (with the degradation named), or blocks multi-worker.
- The `--workers 1` pin cites this item, so the next reader finds the list
  instead of the warning alone.

## Target + blast radius

Inventory only: `src/precis/serve_ledger.py`,
`src/precis/_pagination.py`, `src/precis/install_watchdog.py`,
`src/precis/server.py`, `src/precis/store/pool.py`.

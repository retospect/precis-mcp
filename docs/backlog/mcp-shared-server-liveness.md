---
status: draft
title: Nothing detects a shared session MCP that is up but wedged — the recovery design only covers crash and clean exit
prio: normal
pillar: platform
---

# Nothing detects a shared session MCP that is up but wedged

## Motivation / why

The shared server's recovery story has exactly two branches, and both
assume the process *stops*: the checkout watchdog exits cleanly on a HEAD
move and Docker's `--restart unless-stopped` starts a fresh one, and a
crash does the same via the exit breadcrumb. `install_watchdog`'s own
module docstring names the third state as the reason it exists — a server
whose code was swapped underneath it "doesn't fail fast — it desyncs at
the protocol level ... and then hangs until the client's 1800 s idle
timeout". That state is a process that is running, listening, and useless.

Nothing notices it. There is no `HEALTHCHECK` on the container, no probe,
and `--restart unless-stopped` only acts on exit. Under the old
per-session containers a wedge cost one session, and the operator noticing
was the detection mechanism. Shared, a wedge costs every session at once,
and the "operator notices" path is worse rather than better — twelve
sessions each see one dead tool, and none of them owns the server.

This is the cost `session-mcp-http-server.md` disclosed as "blast radius
inverts and this is the main cost of the change". It named criterion 1 plus
the restart policy as the mitigation. Dogfooding showed the mitigation is
narrower than claimed: it covers exits, not wedges.

## In scope

- A liveness probe that exercises the MCP path, not just the socket — a
  bound port proves nothing here, and `/healthz`-style checks have already
  lied on this fleet (`embedder_wedged_warming`: healthz OK while the
  model never finished warming). An `initialize` round-trip with a short
  timeout is the cheapest honest signal.
- A `HEALTHCHECK` in the container (or an equivalent in the ensure script)
  wired so an unhealthy verdict actually restarts the process — Docker's
  restart policy does not act on health by itself, which is the trap.
- A decision on what a failing probe should do: restart immediately, or
  restart only after N consecutive failures. A shared server that
  restart-loops under load is worse than one that is briefly slow.

## Explicitly NOT in scope

- Alerting or a dashboard. The fleet's alert path is a separate concern
  and this item is about the local dev machine's one container.
- The cluster daemons. They keep stdio and the `site-packages` install
  watchdog.
- Changing the checkout watchdog. It handles the case it was built for
  correctly; this is the adjacent case nobody handles.

## Acceptance criteria

- A server made deliberately unresponsive (SIGSTOP the process, or a
  blocked dispatch) is detected and replaced without a human noticing
  first. Demonstrated, not inferred.
- A healthy server under a 12-session burst is **not** restarted — the
  probe must not mistake load for a wedge. Use the measured numbers in
  `session-mcp-http-server.md` (12 concurrent searches, 4.78 s wall) as
  the floor for any timeout.
- A restart loop is visible somewhere an operator will look.

## Target + blast radius

`~/work/infrastructure/precis-mcp/scripts/precis-mcp-http-ensure.sh`,
`docker/Dockerfile` (dev target) if the probe lands as a `HEALTHCHECK`.
No change to `precis serve` expected — if one turns out to be needed, that
is a signal the probe is reaching too far in.

---
status: idea
pillar: platform
---

# The local gate holds the ship lock for its whole run, and serialises the fleet

`scripts/ship`'s LOCAL gate path takes the ship lock before the gate and holds
it until the CAS push — 1h43m on the `--mutate --full --slow` lane. Every
sibling `scripts/ship` in any mode, including `--quick`, parks for the whole
duration.

`--remote` already does the right thing and says why: it takes the lock only
around the final CAS push, because holding it across a GitHub matrix "would
starve every sibling ship". The local path does exactly that, for longer.

## Measured, 2026-09-29

Two incidents in one day:

1. A `/qgo`'s `--quick` ship parked on the lock held by a running `/go`.
2. A `/go` ran the full slow gate green (23732 passed, 77 skipped, 6 xfailed,
   1h43m) and then **lost the CAS** — `origin/main` had advanced to
   `4cd832f52` during the gate, a real cross-package refactor. Ship correctly
   re-synced and started attempt 2 of 3. Meanwhile a sibling `--quick` had
   been parked on the lock for ~50 minutes, and would have landed the instant
   the lock released.

The second is the shape that matters: the lock does **not** prevent the CAS
loss it exists to prevent, because the session that moved main was not holding
it — it had already landed and exited. So the fleet paid full serialisation and
got the re-gate anyway. Gate 2 was abandoned; nothing was gated that day after
`f8f884d15`.

## Why the obvious fix is wrong

"Skip the lock for `--quick`" was the first idea and it is worse: main then
moves freely under a running gate, so every gate loses its CAS and re-runs. At
nine in-flight sessions that livelocks gates instead of ships.

## Fix sketch

Port `--remote`'s bounded policy to the local path: run the gate unlocked,
take the lock only around the CAS push, and bound the re-gate count the way
`PRECIS_REMOTE_CI_RETRIES` does. The gate re-running when main moves is
correct and already implemented (attempt N/3 with a hybrid fallback) — the
lock is not what provides that guarantee, the CAS is.

Consider also: a gate that has already produced a green result over tree T
does not need a full re-run when main moves to a tree whose diff against T
touches nothing the gate exercised. Today's re-gate was justified (a module
move), but a docs-only advance would have cost the same 1h43m for nothing.

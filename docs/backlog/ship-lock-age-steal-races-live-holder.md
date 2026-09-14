# `scripts/ship`'s 30-min lock steal fires on a LIVE holder

**Hit 2026-09-14.** A `/go` settle-up stole the ship lock from
`imperative-honking-rose` pid 70601 — a legitimate `scripts/ship --mutate`
run 1h10m into its full gate — and announced "assuming crashed or from
another host". Neither was true: the process was alive on this host. Two
concurrent ships then ran until the steal was noticed and the second was
killed by hand.

## The defect

`acquire_ship_lock` has two reclaim paths (`scripts/ship`, §3 "acquire the
ship lock"). They are documented as ordered — pid-dead first, age as the
"fallback for when the pid check *can't tell* (holder file
missing/unparseable, or the lock came from a different host)". The code does
not implement that condition:

- the pid-dead branch `continue`s when `kill -0 "$holder_pid"` **fails**, so
  control only reaches the age branch when the pid is parseable *and alive*;
- the age branch then steals anyway, on lock-dir mtime alone.

So "the pid check couldn't tell" is never actually tested. Any ship whose
lock is held longer than 30 minutes gets stolen — and `--mutate` plus a full
gate routinely exceeds that (the observed holder was at 70 min and still
healthy). The mkdir-mutex exists to prevent exactly the
`shared-index-ship-race` clobber its own header comment cites, so this
silently re-opens that race on every slow ship.

Aggravating: mtime is stamped once at `mkdir` and never refreshed, so the
timer measures *lock age*, not *lack of progress*.

## Fix

1. **Gate the age steal on the pid check being unable to decide** — steal on
   age only when `holder_pid` is empty/unparseable, or the holder is known to
   be from another host. A non-empty pid that answers `kill -0` means a live
   local holder: wait, never steal.
2. **Record the hostname in the holder file** (`printf '%s pid=%s host=%s'`).
   Today "from another host" is unknowable — pids are host-local and the
   holder file omits the host — which is the ambiguity the age branch was
   papering over. With a host field, the two paths become decidable: same
   host → pid check is authoritative; different host → age steal is correct.
3. **Heartbeat the lock** (`touch "$LOCKDIR"` each phase, or from the gate
   loop) so a retained age-based fallback means "no progress for 30 min"
   rather than "started 30 min ago".

(1) alone removes the observed failure; (2) is what makes the fallback
correct rather than merely narrower; (3) is defence in depth.

## Test

`acquire_ship_lock` is shell, so cover it the way the other shell paths are:
a holder file naming a live pid (use `$$` of a `sleep`), a lock dir aged past
30 min via `touch -t`, and assert the acquire **waits** rather than stealing;
plus the complements — dead pid steals immediately, unparseable holder steals
on age.

## Do not fix while ships are in flight

`scripts/ship` runs `git merge origin/main` mid-run, so a sibling's live ship
can have its own `scripts/ship` rewritten underneath it — bash reads a script
incrementally by offset, so an in-place change mid-execution can corrupt the
remainder of the run. Land this only when `scripts/inflight` shows no
concurrent ship, or accept that in-flight siblings may need a re-run.

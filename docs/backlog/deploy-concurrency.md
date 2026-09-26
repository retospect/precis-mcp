---
status: draft
---

# Deploy concurrency

Grouped 2026-09-26 from 2 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## The lock that two people can hold

_Grouped 2026-09-26; was `deploy-lock-steal-is-not-atomic`, status draft._

`scripts/deploy`'s mutex (`_acquire_deploy_lock`) exists because two
overlapping deploys interleave their installs: each run pins its own sha, so
the later run's installs move the venvs under the earlier run's convergence
assert, and a healthy cluster reports `DEPLOY DID NOT CONVERGE`. That is
`gr203786`, observed twice on 2026-08-11, and the lock was the fix.

The lock closes the common case and leaves one hole open.

### The race

```bash
while ! mkdir "$_LOCK_DIR" 2>/dev/null; do
    holder="$(cat "${_LOCK_DIR}/pid" 2>/dev/null || true)"
    if [[ -n "$holder" ]] && ! kill -0 "$holder" 2>/dev/null; then
        rm -rf "$_LOCK_DIR"      # ← unconditional
        continue
    fi
    ...
done
echo $$ > "${_LOCK_DIR}/pid"     # ← not atomic with the mkdir above
```

`mkdir` is atomic, so the *acquire* is sound. The **steal** is not: the
staleness check and the `rm -rf` acting on it are separate steps, with no
re-verification that the directory being removed is still the one whose pid
was read.

| | B | C |
|---|---|---|
| 1 | reads `holder=A`, A is dead | reads `holder=A`, A is dead |
| 2 | `rm -rf`; `mkdir` **succeeds**; writes `pid=B` | |
| 3 | *(running the deploy)* | `rm -rf` — **removes B's live lock** |
| 4 | | `mkdir` succeeds; writes `pid=C` |

B and C now both believe they hold the lock, and the trap on either one's
exit removes whatever directory is present — possibly the other's. This is
precisely the concurrent-deploy condition `gr203786` describes.

A second, narrower gap: `echo $$ > pid` runs *after* `mkdir` returns, so a
waiter that reads the pid file in that window sees it empty. That one is
benign — empty `holder` fails `[[ -n "$holder" ]]`, so the waiter waits
rather than steals — but it means the pid file is not a reliable witness of
ownership, which is what the steal path depends on.

### Why we think it fired on 2026-08-21

A `/go` deploy pinned `4ca74e10` and failed its convergence assert on three
venvs (balthazar + spark `/opt/precis/embedder-venv`, melchior `/opt/mcps/venv`
and `/opt/precis/venv`), all reporting `bf8ecfb43c`.

The assert's own `fail_msg` rules out the actual cause:

> The target is frozen for this run, so this means the install was genuinely
> skipped/failed on this venv (re-run), not a main that moved under it.

**`bf8ecfb43c` is newer than the pinned `4ca74e10`.** A skipped or failed
install cannot leave a venv on a *future* commit — only another installer
running from a later pin can. `bf8ecfb43c` was the tip of `origin/main` at
the time, shipped mid-run by the sibling worktree
`expressive-roaming-pizza`. Cluster daemons were observed bouncing ~20
minutes *after* the failing run ended, consistent with a second full deploy
overlapping the first.

Not reproduced deterministically — the interleaving above is the mechanism
that fits the evidence, not a captured trace.

### `scripts/ship` has the same steal race — and half the fix already

Noted 2026-08-21 while waiting on the ship lock. `scripts/ship`'s
`precis-ship.lock.d` mutex is the same shape: read `holder`, `kill -0` it, then
an unconditional `rm -rf` that is not atomic with the `mkdir`. Two waiters can
still both acquire.

Its **release** path, though, is what `scripts/deploy` should copy:

```bash
if [[ "$holder_pid" == "$$" ]]; then      # positive ownership match
    rm -rf "$LOCKDIR" 2>/dev/null || true
fi
```

with the reasoning already written down beside it — a missing or unparseable
holder is ambiguous (ours with a failed write, or a sibling mid-steal), and
deleting a sibling's live lock has no recovery while leaking our own self-heals
via the staleness steal. `scripts/deploy`'s trap removes whatever directory is
present, unconditionally, which is the second half of the race table above.

So the fix has an in-repo precedent, and the two lock implementations should
end up sharing one. Fixing only `deploy` leaves `ship` able to double-acquire —
and a double ship is the shared-`.git/index` clobber that
`shared_index_ship_race` documents.

### Work

1. **Make the steal atomic.** Applies to `scripts/deploy` **and**
   `scripts/ship`. Acquire identity and directory in one step
   rather than two: `mkdir` a uniquely-named dir and `rename`/`ln` it into
   place, or re-read the pid immediately after a successful `mkdir` and
   abort if it is not ours. A steal must fail when the directory it targets
   is no longer the one it inspected. Give `scripts/deploy`'s trap the
   ownership check `scripts/ship` already has.
2. **Fix the assert's `fail_msg`.** It sends the operator to "re-run" while
   explicitly denying the cause. It should compare the installed sha's
   *ancestry* to the pinned one and say so: installed-is-a-descendant means
   a concurrent deploy, installed-is-an-ancestor means a genuinely skipped
   install. Only the second warrants a re-run.
3. **Consider making a descendant install non-fatal.** When every venv is on
   one sha that *contains* the pinned target, the cluster is uniform and
   newer — arguably a pass with a warning, not a failure. Decide
   deliberately; a uniform-but-unexpected cluster and a mixed-version
   cluster are different states and only the latter is dangerous.
4. Close `gr203786` only once 1 is done — the lock it tracks is incomplete,
   not wrong.

## Concurrent deploys race, and the assert misdiagnoses the result

_Grouped 2026-09-26; was `concurrent-deploys-race-and-misdiagnose`, status draft._

Hit 2026-08-22 deploying `507975cd`. Every venv on every host failed the
convergence assert:

```
melchior: /opt/precis/venv is on 5d5cc72e24 but the pinned deploy target is
507975cd80 — DEPLOY DID NOT CONVERGE. The target is frozen for this run, so
this means the install was genuinely skipped/failed on this venv (re-run),
not a main that moved under it.
```

`5d5cc72e` was a **sibling session's ship, deployed concurrently** — one commit
newer than my pin, and a descendant of it. Nothing was broken: all venvs agreed
on one sha, the cluster was not mixed, and the newer sha contained my commit.
The deploy still exited non-zero.

### Two separate problems

#### 1. The message asserts the one thing that was true

"The target is frozen for this run, so this means the install was genuinely
skipped/failed on this venv (re-run), **not a main that moved under it**." The
pin *is* frozen per-run (step 0's `ls-remote` → `set_fact`), so the reasoning is
locally valid — but it silently assumes this run is the only writer. Under a
concurrent deploy, "main moved under it" is exactly what happened, via another
ansible process installing into the same venvs.

Cheap fix: when installed ≠ pinned, test ancestry before concluding. If the
installed sha is a *descendant* of the pin, say so — "a newer deploy
(`<sha>`) landed here mid-run, probably a concurrent `scripts/deploy`; the
cluster is uniform and ahead of this target" is a different situation from a
failed install, and only one of them wants a re-run. Worth reporting whether
all venvs agree, too: uniform-but-ahead is benign, disagreement is not.

#### 2. Nothing prevents the race

`scripts/deploy` takes no lock. Two sessions can bounce the same daemons and
install into the same venvs simultaneously; the interleaving that produced this
was benign, but an install landing mid-bounce on the *other* run's daemon
restart is not obviously safe. A cluster-wide advisory lock (a PG advisory lock
on the DB node is already reachable, and now provably queryable — see the drain
preflight) would make the second deploy wait or refuse rather than interleave.

Related: this is why the staleness guard fired on the *first* attempt of the
same session (`local tree does not contain origin/main (e408f94b vs 507975cd)`)
— that guard works and is not in question here.

### Same shape as the drain bug

Both are deploy-path checks that produce a confident, specific, wrong
conclusion — the drain by conflating "could not ask" with "nothing to wait
for", this one by conflating "install failed" with "someone else installed
something newer". A check that cannot distinguish its failure modes hands you
a diagnosis instead of a fact. See `deploy-drain-wait-is-a-silent-noop.md` and
memory `psqlrc_pollutes_scripted_psql`.

### Verification

Reproduce by running two `scripts/deploy` invocations against different pins
and confirming the loser reports the ahead/behind relationship rather than a
flat "DID NOT CONVERGE". For the lock: the second invocation should block or
refuse with the holder's identity, not interleave.

---
status: draft
title: "the deploy lock's dead-holder steal is a TOCTOU race — two waiters can both acquire, re-opening the concurrent-deploy window it was built to close"
---

# The lock that two people can hold

`scripts/deploy`'s mutex (`_acquire_deploy_lock`) exists because two
overlapping deploys interleave their installs: each run pins its own sha, so
the later run's installs move the venvs under the earlier run's convergence
assert, and a healthy cluster reports `DEPLOY DID NOT CONVERGE`. That is
`gr203786`, observed twice on 2026-08-11, and the lock was the fix.

The lock closes the common case and leaves one hole open.

## The race

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

## Why we think it fired on 2026-08-21

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

## `scripts/ship` has the same steal race — and half the fix already

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

## Work

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

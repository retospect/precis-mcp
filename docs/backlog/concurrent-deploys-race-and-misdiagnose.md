---
status: draft
title: "two sessions deploying at once produce a spurious DEPLOY DID NOT CONVERGE whose message explicitly rules out the real cause"
---

# Concurrent deploys race, and the assert misdiagnoses the result

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

## Two separate problems

### 1. The message asserts the one thing that was true

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

### 2. Nothing prevents the race

`scripts/deploy` takes no lock. Two sessions can bounce the same daemons and
install into the same venvs simultaneously; the interleaving that produced this
was benign, but an install landing mid-bounce on the *other* run's daemon
restart is not obviously safe. A cluster-wide advisory lock (a PG advisory lock
on the DB node is already reachable, and now provably queryable — see the drain
preflight) would make the second deploy wait or refuse rather than interleave.

Related: this is why the staleness guard fired on the *first* attempt of the
same session (`local tree does not contain origin/main (e408f94b vs 507975cd)`)
— that guard works and is not in question here.

## Same shape as the drain bug

Both are deploy-path checks that produce a confident, specific, wrong
conclusion — the drain by conflating "could not ask" with "nothing to wait
for", this one by conflating "install failed" with "someone else installed
something newer". A check that cannot distinguish its failure modes hands you
a diagnosis instead of a fact. See `deploy-drain-wait-is-a-silent-noop.md` and
memory `psqlrc_pollutes_scripted_psql`.

## Verification

Reproduce by running two `scripts/deploy` invocations against different pins
and confirming the loser reports the ahead/behind relationship rather than a
flat "DID NOT CONVERGE". For the lock: the second invocation should block or
refuse with the holder's identity, not interleave.

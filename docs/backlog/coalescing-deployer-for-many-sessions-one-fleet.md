---
status: idea
title: N sessions land in an hour and each gets its own deploy — consider coalescing, but only if it still hurts
---

# Many sessions, one fleet: should deploys coalesce?

## What
On 2026-09-26 five sessions had work to land. Each finished batch wanted a
deploy, and the fleet got four in a day. Nothing coalesced them: a session
lands, runs `/go`, and deploys the tip it just made.

The obvious shape is a **coalescing deployer**: sessions land on `main`, one
owner process periodically deploys the newest *gated* `main`, and N merges
inside a window collapse into one run.

## Why this is filed as "maybe", not "todo"
Two changes the same day removed most of the motivation, and building a
queue against the old numbers would be solving a problem that no longer
exists:

- **The render-worktree change (709faf58)** means a merge to `main` can no
  longer invalidate an in-flight deploy. Sessions do not have to hold their
  work while someone else deploys. That was the actual serialization cost on
  09-26 — one session held ~90 minutes for it.
- **The fast gate (b9bb05e0)** took the local gate from 2h08m to ~11m30s
  (measured three times: 694s, 706s, 686s). A follow-up `/go` for a batch
  that missed the last one is now cheap rather than an afternoon.

And the deploy itself is not inherently slow: the 13:04 run took **7m36s**
end to end. The 94-minute and 61-minute runs earlier the same day were one
bad step, tracked separately in
`agent-image-build-stalls-on-mirror-fallback.md`.

So the residual waste is "we bounced every daemon four times instead of
once", which is real but modest, and much of the apparent pain was the
agent-image stall wearing a queueing costume.

## What would change the answer
Revisit if, after the agent-image stall is fixed, we still see: several
deploys per day whose only difference is a few commits, or sessions idling
on the deploy lock rather than on their own work. Until then the cheap
fixes are doing the job.

## If it is built
It needs an owner process and a machine-readable "this sha is gated" marker
(today gatedness lives in a check.yml run's conclusion plus `.ship-sha`,
neither of which a third party can query cleanly). That marker is probably
worth having on its own, independent of coalescing.

## Not in scope
The deploy lock. Serializing the fleet mutation itself is correct and is not
what this item questions.

# Memory index

## Threads

- [Alpha campaign](alpha-campaign.md) — shipped and verified; next step is the beta rollout once the long-running calibration finishes
- [Beta rollout](beta-rollout.md) — blocked on the alpha calibration, resume from the checklist in the file
- [Gamma redesign](gamma-redesign.md) — design settled, build not started

## Runbooks

- [Restart the worker](restart-worker.md) — stop, drain the queue, start; never kill mid-batch
- [Rotate the token](rotate-token.md) — new token first, then revoke the old one

## Gotchas

- [Cache poisoning](cache-poisoning.md) — a stale entry looks like a fresh one; clear by key, not by age
- [Clock skew](clock-skew.md) — timestamps from the second host run ahead by a few seconds
- [Lost notes](lost-notes.md) — this file was never written down, so the importer falls back to the bullet text

## Workflow

- [Commit style](commit-style.md) — one-line subject, no body
- [Review habit](review-habit.md) — read the failing line before opening the log

## Reference

- [Glossary pointer](glossary-pointer.md) — where the coined terms live

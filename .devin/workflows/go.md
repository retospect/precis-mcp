---
description: Implement the agreed spec, ship to main with the local gate, deploy the gated sha to the cluster (scripts/deploy --pinned).
---

The procedure is `.claude/commands/go.md` — read it and follow it. It is the
single source; this file only maps what differs for Devin.

## Devin differences

- **No subagents.** Where the procedure dispatches an agent (risky-diff
  `reviewer`, background mutation pass, `issue-closer`), do the review
  yourself before shipping and skip the background passes; name them in
  your summary.
- **Deploy the gated sha, never bare `scripts/deploy`:**
  ```
  scripts/deploy "$(cat .ship-sha)" --pinned
  ```
  Bare `scripts/deploy` re-resolves `main` and can ship an ungated sibling
  land. No `.ship-sha` → do not deploy.
- **Never pipe** `scripts/ship` or `scripts/deploy` into `tail`/`grep`/`tee`;
  redirect to a log.
- **Open round:** if `scripts/round status` shows one, the coordinator
  deploys — land, mark `scripts/round in <sha>`, and do not deploy yourself.

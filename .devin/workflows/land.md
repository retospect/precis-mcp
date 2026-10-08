---
description: End-of-session wrap-up — commit WIP, sync onto main, gate on GitHub CI (scripts/ship --remote), squash-merge to main, mark the open round. Ship-only; /go also deploys.
---

The procedure is `.claude/commands/land.md` — read it and follow it. It is
the single source; this file only maps what differs for Devin, so the two
cannot drift apart again.

## Devin differences

- **No subagents.** Where the procedure dispatches an agent (risky-diff
  `reviewer`, background `issue-closer`), do the review yourself before
  shipping and skip the issue-closer; list what it would have closed in your
  summary.
- **Run ship in the background with output to a log**, never piped:
  ```
  scripts/ship --remote "<message>" > /tmp/ship.log 2>&1
  ```
  The remote gate takes ~90 min green. A pipe reports the filter's status,
  so a red gate would read as exit 0.
- **Prod writes.** Filing a `gripe`/`todo` through the precis MCP writes
  PROD — only with the user's explicit go-ahead.

## After a green land: mark the round

If `scripts/round status` shows an open round, mark it from this tree:
`scripts/round in <sha>` (or `none`, or `eta <text>`). The mark is your
status report; message the coordinator only for a question, blocker or
decision.

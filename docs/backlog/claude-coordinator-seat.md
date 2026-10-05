---
status: idea
title: A Claude session in the Codex fleet's coordinator seat
pillar: platform
---

# A Claude session in the Codex fleet's coordinator seat

Reto, 2026-10-04: Codex workers do bounded coding well enough when a
stronger model runs the show. Seat 0 of `.claude/fleet/codex.tsv`
(`coordinator`) is a Codex model today. This item puts a Claude session
there. Roster, launch and safety bounds stay as
[codex-fleet-bootstrap](codex-fleet-bootstrap.md) has them.

## What is missing

- **Worker to coordinator.** `scripts/fleet-codex` `Fleet.send` delivers with
  `codex queue --thread`, and `Fleet.register` requires a Codex thread id for
  seat 0. A Claude session in window 0 has no thread id, so no worker can
  address it. Change: for seat 0, `send` writes one file per message into an
  inbox under the shared state directory (workers already hold it through
  `--add-dir`), and the coordinator watches that directory.
- **Coordinator to worker** needs nothing: `scripts/fleet-codex send <name>
  --message …` runs from any shell.

Estimate: about 30 lines in `scripts/fleet-codex` plus a test, one build.

## How the coordinator works

1. One bounded slice per worker, taken from its thread file, with a test as
   the acceptance check.
2. On hand-back the diff goes to a reviewer subagent or `codex exec review`.
   The coordinator reads the verdict, not the diff, so its context stays
   small.
3. The coordinator decides integration order, lands, and runs the round
   (`scripts/round`). A question for Reto becomes a review-queue item.

Start with 4 to 6 workers. Every hand-back costs coordinator context, and
27 at once is unmeasured.

## Why

Read from `scripts/inflight` on 2026-10-04: about ten `codex-*` branches
sat ahead of main with no live session, several with uncommitted files;
three hand-built integration branches existed; releases r6 and r7 each
needed a fix-forward. Integration is where the work piled up. The cause of
each stall was not traced.

## Not doing

- No MCP bridge between the two CLIs. `codex mcp-server` is absent from
  codex-cli 0.160.0. The community wrappers shell out to `codex exec`, which
  Bash already does, and an MCP call is request and response, so a worker
  cannot start a message.
- Codex runs without the Claude Code hooks. Its output takes the full gate,
  never `/qgo`.

test: with seat 0 unregistered, `fleet-codex send coordinator --message x`
writes one inbox file and exits 0; a worker seat still queues through
`codex queue`.

Measure: landed slices per day and rounds to green against the
Codex-coordinated run of 2026-10-04.

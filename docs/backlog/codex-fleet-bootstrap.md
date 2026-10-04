---
status: in-progress
pillar: platform
---
# Codex fleet bootstrap and process learning

Reto approved the window plan, one task branch/worktree per worker, coordinator
0, communicator 1, gripe-fixer 25; added Astra architecture review 26 and a
six-hour transcript review. The roster is `.claude/fleet/codex.tsv`.

## Scope and acceptance

- Launch one Codex session per agreed window with the selected model/effort.
  Pilot registration, MCP connectivity and native message delivery before
  expanding. Preserve occupied windows and all dirty worktrees.
- Workers build bounded slices and report to coordinator; only coordinator
  authorizes integration/deploy. No edits/commits in primary main. Existing
  holds remain explicit. Shared files have one assigned writer.
- Persist decisions, compact worker handoffs, deployed SHAs, findings, and
  before/after measurements in shared fleet state; promote durable procedures
  into runbooks and graph memory when its tested workflow can own them.
- Queue a review every six hours, with persistent interval bounds and one
  pending review. Advance the checkpoint only on a completed evidence report
  with fixes or owned follow-ups, not on timer firing.
- Mine this project's Codex transcripts including worker sidechains; reuse
  normalized/redacted evidence cards. Track coverage, actual token accounting,
  retries, tool-output size, context rereads and instruction/MCP confusion.
- The architecture reviewer works read-only against implementation and
  operational evidence, reporting severity, code anchors and focused remedies.

Launcher and review commands: [codex-fleet](../runbooks/codex-fleet.md) and
[codex-fleet-review](../runbooks/codex-fleet-review.md). This item remains open
for staged roster expansion, verified runtime access and the first completed
review interval. Release-head tooling is implemented; combined gating and
end-to-end live release acceptance remain coordinator-owned.

## Safety and resource bounds

Use Codex native queue for agent messages, never terminal keys in permission
dialogs. Automatic approval review may evaluate routine commands; coordinator
cannot override sandbox/approval refusals. Workers report exact blocked action
and reason. No Docker prune; no shared /tmp scripts. Heavy tests use scripts/test
and its common queue. Raw transcripts/artifacts stay outside the repo.

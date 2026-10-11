---
status: draft
pillar: platform
prio: high
---

# Fleet token efficiency: Claude versus Codex

Requested by Reto 2026-10-04 after observing roughly 20% of weekly
allocation used without a successful integrated build. Read-only first
audit; no launch/model/config changes or deployments authorized by this
document. Goal: accepted, verified output per allocation unit, preserving
review quality and existing research holds.

## Evidence and boundaries

Scanned 48 local Codex session files dated October 4, 72.8 MB at first
capture, whose cwd contains precis-mcp. Includes automatic approval-review
sessions and bootstrap probes, not 48 human-facing workers. Streaming JSON
aggregation exposed only model/effort, token counters and tool-size/count
metadata; no raw conversations or credential values exported.

Model choices recorded in turn context:

| Assignment | Observed model / effort |
| --- | --- |
| Coordinator | Sol/default or low; Astra/extra-high |
| Main architecture reviewer | Astra/extra-high |
| Graph, knowledge, paper, catalysis | Astra/high |
| Release, MCP, Drive, local compute | Sol/high |
| Communicator, gripe fixer, SE | Sol/medium |
| Housekeeping | Luna/medium |
| Automatic command reviewer | codex-auto-review/low |

These are observed choices, not evidence of equal task cost. The global
Codex config defaults to Astra/extra-high, but the coordinator did tier the
fleet. Configuration changes would affect future launches unless overridden;
do not assume they change running sessions.

First tool scan: 1,182 custom orchestration calls containing 1,758
`exec_command(` occurrences. One custom call referenced `rtk`; 330 contained
`sed -n`, 67 contained `tail`, 200 contained wait/poll helpers. Tool-result
serialization totaled ~10.54 MB; 191 individual results exceeded 20 KB.
These counts describe returned metadata/text, not billable tokens. Commands
may contain multiple reads and multiple searches. Tails and waits can be
legitimate; this scan does not establish redundant polling or exact repeated
file reads. No claim that every large output is waste.

Input caching is substantial in recorded usage. Summing per-response usage
requires reconciling resumed session shards and model billing semantics;
raw input totals must not be presented as fresh tokens or converted into
weekly subscription percentages. No provider allocation attribution or
matched Claude-versus-Codex task benchmark yet.

## Settings and hooks compared

- Claude user settings select `fable` and install the Bash RTK shim.
  Project settings add session memory/index loading, context-size nudges,
  precompact persistence, command/read/secret/worktree guards and delegation
  nudges. Wiring exists; this audit does not prove each hook ran today.
- Codex user config selects Astra/extra-high. Inspected user and project
  configs have no inline hooks, and user, primary-project and communicator
  `hooks.json` files were absent. Other config layers/plugins/worker-local
  hooks are not exhaustively ruled out.
- Claude's RTK shim intentionally skips commands mentioning `git` inside
  worktrees because of isolation checks. Claude does not compress all output.
- Existing Claude token-review history already records repeated file reads,
  compaction thrash and oversized image reads. Copying its whole startup
  stack would copy overhead as well as useful protections.
- Current official Codex docs support command hooks, including tool-input
  rewriting and compaction events. Port inputs, outputs, matchers, trust and
  path handling deliberately; do not copy Claude settings or approval
  handlers blindly. Nested orchestration coverage needs an actual probe.
  Sources: https://learn.chatgpt.com/docs/hooks and
  https://developers.openai.com/plugins/guides/submit-claude-plugin .

## Delivery assessment

Latest available integration checkpoint: first full suite had 5 failures,
27,691 passes. Four failures were caused by deleting collected spec paths
during the run; one by seven fixture text-I/O calls omitting encoding.
Corrected focused suite: 676 passes; typecheck 2,478 files and Ruff passed.
A second full run was authorized, pending coordinator recovery at that
checkpoint. Subsequent coordinator update confirms the corrected full
suite ran in detached tmux `precis-fleet-integration-test` and passed
with exit code 0 at 13:28:27 UTC (shared `fleet-integration-full-run.json`).
Round 6 remains OPEN at base `e0b75bdc7c4b`, with no frozen candidate.
Last verified runtime is 8.35.1 at that same SHA. This closes the
bootstrap full gate; it is not the combined graph/Drive/release-tooling
candidate gate or deployment evidence.

Graph independently reports 205 focused passes and source review closing
G1; release source review reports PASS. These are substantive outputs,
but neither substitutes for an integrated release gate. Editing gate
inputs mid-run is demonstrated avoidable rework. Source approval and
focused green must not be counted as successful deployments.

## Recommended next experiment, coordinator-owned

### Approved hook rollout (2026-10-04)

Reto explicitly approved adding RTK reminders/compression/context hooks here
and making them available to all worktrees. Implement a user-level Codex
hook definition referencing this worktree's committed stdlib adapter, scoped
to precis-mcp sessions. SessionStart/PostCompact give a short context diet;
PreCompact requests a durable handoff. PreToolUse compresses only simple
read-only git status/log and whole `.log` reads with installed RTK, leaving
searches, compound commands, escalated calls and mutations untouched.
Rate-limited reminders cover log handling and large output. Worktree-local
metrics contain counts/byte lengths only, never commands or responses.
No PermissionRequest hooks, no automatic approval/trust manipulation, no
model changes or production deployment. Actual hook trust/loading is a
platform activation gate, distinct from tests/config installation.

Reto additionally requested using the loaded MCP's source-navigation skill.
Native `get(skill, precis-python-help)` and `search(python, StoreCore,
scope=precis)` verified readable symbol hits. Startup and rate-limited
source-search reminders now point to that skill, native search and symbol
views. Confirm the served alias/root matches the task worktree; retain raw
rg for exhaustive checks or unindexed/mismatched sources. No MCP source
writes or root reconfiguration; changing a file in another checkout remains
prohibited. Existing sessions need hook reload evidence before claiming use.

1. Hold further fleet expansion while converting existing ready work into
   one immutable-tree full green gate. Preserve running work and holds;
   audit does not authorize cancelling workers.
2. Trial compact command/log output on one bounded Sol worker. Keep full
   logs in its worktree; show failure/summary slices and preserve exit codes.
   Use raw search when completeness matters. Confirm hook trust and nested
   exec coverage before calling it automatic RTK integration.
3. Trial medium reasoning on a bounded implementation/support assignment;
   retain Astra/high for domain/design calls and independent high-effort
   release review when justified. Compare repairs, acceptance and latency.
   This is a recommendation, not an approved worker model change.
4. Make startup/handoff compact and task-specific; retain governing safety
   rules but fetch leaf docs on demand. Avoid rereading entire thread notes.
5. Extend existing transcript miner to separate provider counters, cached
   input, reasoning/output, tool payload, compactions and repeated reads.
   Dedupe responses, distinguish approval subthreads, attribute to task/SHA.

Report tokens/allocation per accepted change, successful gate, repaired
defect and completed research output; include discarded work and retries.
For paired tasks record model/effort, input/cache/output, result acceptance,
failures, time and exact output-reduction mode. No savings percentage until
those measurements exist. See `token-review-hook-gaps.md` and
`docs/runbooks/token-review.md` for the existing Claude workflow.

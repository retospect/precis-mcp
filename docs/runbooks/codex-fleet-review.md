# Codex fleet review every six hours

Reto requested this cadence for the active Codex fleet. Existing seven-day
token and fourteen-day MCP surface passes remain useful for the broader corpus;
cross-reference their findings instead of filing duplicates.

`scripts/fleet-review` is a stdlib clock, not a judge. Its `--state` points to
the fleet control directory, with `registrations/housekeeping.json` containing
the worker's `thread_id`. `init` sets the first due time six hours ahead and
preserves an existing clock. `watch` ticks every minute, logs state changes and
stops on SIGINT/SIGTERM. The coordinator launches it in a persistent tmux pane;
there is no cron installation. `status` shows the high-water mark and pending
interval. No watcher runs merely because these files exist.
Use `init --started-at <ISO timestamp with offset>` when fleet launch preceded
timer setup; the default is initialization time. Registration IDs must be UUIDs.

```sh
scripts/fleet-review --state /path/to/fleet-state init
scripts/fleet-review --state /path/to/fleet-state watch
scripts/fleet-review --state /path/to/fleet-state status
```

One due interval is frozen and queued to housekeeping with native `codex queue`.
Queue errors preserve it and retry after five minutes. A delivery timeout or
process crash leaves uncertain delivery: inspect the worker before using
`tick --retry`. Enqueue success does not advance the clock. Missed intervals
are processed sequentially after explicit completion; each starts at the last
completed high-water mark and ends six hours later.

Housekeeping mines only registered threads and their children with explicit
UTC `[since, until)` bounds, then sends the coordinator the scoreboard, coverage
and compact evidence cards. All extraction artifacts and reports go outside
the repository under a unique `MINE_OUT` directory. Raw transcripts stay local
and never enter a main-loop prompt or committed file.

The coordinator judges MCP/instruction confusion, repeated reads and retries,
delegation and token use. Check the known gripe/backlog set and deployed fixes
before filing. Fix bounded wording or instructions in an isolated worktree;
give larger work an owner and acceptance test. Keep compact process notes in
the control directory and enduring instructions in their owning repository doc.

Completion requires an external JSON report, not merely a successful mining run:

```json
{
  "review_id": "review-20261004T180000Z",
  "since": "2026-10-04T12:00:00Z",
  "until": "2026-10-04T18:00:00Z",
  "coverage": {"events": 120, "missing": ["nested MCP calls inside exec"]},
  "findings": [{"signature": "repeated grammar retry", "evidence": "cards/example.md"}],
  "actions": [{"owner": "mcp-platform", "task": "clarify command grammar", "acceptance": "example call succeeds"}]
}
```

```sh
scripts/fleet-review --state /path/to/fleet-state complete \
  --review review-20261004T180000Z --report /external/review/report.json
```

Empty findings/actions lists are valid after a documented clean review.
Findings require recorded actions. Completion validates identity and exact
interval, persists the report reference and advances the high-water mark.
`coverage.json` limitations remain part of the report: opaque orchestration,
missing token data or absent threads mean incomplete coverage, not zero work.

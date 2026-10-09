---
status: draft
pillar: platform
---

# Fleet coordination via precis

Precis becomes the control plane for agent sessions across hosts, vendors
and projects: who is running where, doing what, how to reach them, and what
needs a decision. The harnesses (Claude Code, Codex, Devin) stay the
engines. Ruled with Reto 2026-10-09.

## Why

- Coordination chatter was ~10% of fleet cache reads on 10-07: 72 inbound
  messages to the coordinator, 51% status, and 23 of those 37 repeated a
  `scripts/round` mark the sender had already written. 41 `ScheduleWakeup`
  ticks. Each wake is a full turn at ~250k context.
- Coordination state is per machine. `scripts/round` keeps marks in
  `<git-common-dir>/precis-round/`; `scripts/inflight` liveness is a local
  pid. Codex on melchior and Claude on the Mac cannot see each other.
- `SendMessage` is Claude-only and `codex queue` is Codex-only. Every pool
  speaks MCP.

## Decisions

- **New `fleet` kind**, not tagged `todo`/`memory`: keeps ~10–40 churning
  rows out of the todo queue, the memory index and dreaming. Opts in to
  `refs.owner_login` (0164) so the view defaults to the caller's rows.
- **Scope:** agent sessions, rounds, and a mailbox. Not precis worker jobs
  (`kind='job'`), not outbound Discord (`kind='message'`), not a chat UI
  (tmux attach is the text interface). Release/deploy logic stays in
  `scripts/round`.
- **One registry across projects.** Every row carries `project` (repo
  top-level name). A project gets its own coordinator only while it has
  several active workers. Round marks apply where `scripts/round` exists.
- **One reporter per host, not per agent.** Agents never heartbeat, so the
  Codex daemon and Devin need no change. Staleness past N minutes = dead.
- **Agent output is not altered.** Transcripts already timestamp every
  entry; the status line (not in context) carries clock and capacity.
- **Fleet sessions run in tmux**, one tmux session per host, windows named
  `project-tree`. Sessions outside tmux (desktop app, IDE) are listed from
  their transcript and marked unreachable.
- **Typing into a pane:** only when the agent is idle at an empty prompt
  (`pane_current_command` is the harness, transcript ends on a finished
  turn), only from a fixed list (`/clear`, `/exit`, `/fleet resume`,
  message text).
  Anything else, approval dialogs included, is an exception reported to
  Reto with the attach line. Fewer dialogs come from config (auto mode,
  `approvals_reviewer = "auto_review"`, allow-lists), never from an
  auto-answerer. Replaces the unexplained "never types into a terminal"
  rule in `docs/runbooks/codex-fleet.md`.
- **Behind main is not an event.** `scripts/ship` merges main at land time.
  The reporter runs `git merge-tree` per branch and raises only a predicted
  conflict or a duplicate migration number.
- **Squash-merge stays.** Out of scope; small slices make it near-lossless.
- **No VM per interactive session** (RAM, credential sprawl, lost shared
  caches). Headless runs get a container each via `sandbox_run` (ADR-0048).
- **No reuse of harness subscription tokens** in a home-built loop (terms
  risk to the main account; loses the model–harness tuning). Plan pricing
  comes from running the harness itself headless.

## Shape

### Reporter (`scripts/fleet-report`)

Runs every ~60 s per host (launchd on the Mac, cron on melchior). Sees,
per session:

- **Git:** worktree, branch, dirty, ahead/behind, last commit,
  `.claude/purpose`; `merge-tree` conflict prediction.
- **Liveness:** worktree-lock pid alive; tmux pane, `pane_current_command`,
  `window_activity`.
- **State** (working / idle / waiting / dead): Claude transcript JSONL
  mtime and whether the last entry is a finished turn or a pending tool
  call; Codex rollout JSONL last event; approval prompt by matching the
  tail of `capture-pane`. Devin: tmux only at first.
- **Capacity:** context fill from per-turn usage in each transcript;
  account quota from Codex rollout `rate_limits` (verify on melchior) and
  for Claude whatever the status-line input exposes (verify).

It writes only changed rows (one cheap last-seen update per host) and
computes the **exception set**: dead, waiting on Reto or approval, quiet
past N minutes, red gate, predicted conflict, unread urgent mail, low
capacity, coordinator past ~40% context. When the set changes it wakes the
coordinator with one message naming the delta; otherwise it is silent.

### Coordinator fisheye

One `get(kind='fleet')`:

```
EXCEPTIONS
  codex@melchior/w7   waiting on approval 14m   attach: ssh … tmux attach -t 0:7
AGENTS  vendor@host/project/tree · state · quiet · purpose · mark · mail · ctx%
QUOTA   claude 5h 42% · codex 5h 18% / week 61%
```

The coordinator never polls. It wakes on Reto, an urgent message, or an
exception-set change. It slices work, assigns, runs one `/go` per round,
and never reads code. Its state lives in `fleet` and round refs, so the
reporter can `/clear` + `/fleet resume` it when idle past ~40% context.

Workers: one slice = one tree = one session; `/qland` on done, handoff
into the `fleet` row, exit. New slices go to the vendor with headroom; a
session past ~60% context finishes its slice and restarts.

### Rounds and session lifecycle

- **What is in.** A round mark links to the agent's `fleet` ref (vendor,
  host, session, attach) and to what it is for:
  `scripts/round in <sha> --for <backlog-slug|grNNN|tdNNN>`, else derived
  from the commit (a touched backlog/thread file, a gripe id in the
  message). The round ref lists sha · who · for what; this feeds the
  stale-cite gate check.
- **No hold by default.** `eta` = wants in; `cut` already prints late
  shas, which ride the next round. An `eta` naming ≤15 min may hold the
  cut once, at the coordinator's choice.
- **The coordinator builds.** Peers never deploy. CI gates every main
  push; the coordinator runs `cut` → `deploy` → verifies the live product
  → `--confirm-runtime`. Only the last two need judgment; a timer can take
  the first two later if the coordinator gets expensive.
- **The coordinator starts and stops sessions.** An idle session costs no
  tokens, but waking one after its prompt cache expired rewrites the whole
  context (1.25–2× input price vs ~0.1× for a warm read: 12–20× dearer).
  A worker exits once its slice is qlanded and its handoff written; one
  waiting more than ~1 h for a deploy exits and a fresh session reads the
  handoff later. The coordinator spawns from the slice queue with the
  existing launchers (`fleet-codex up`, `devin-tree`, `claude -w` in
  tmux); the reporter flags idle + landed + empty-mailbox sessions and the
  coordinator closes them with `/exit` (added to the pane-typing list).
- **Quota source.** Codex `/status` shows the snapshot its rollouts record
  as `rate_limits`; compare the reporter's QUOTA line with `/status` on
  melchior, and fall back to `capture-pane` of `/status` in an idle pane
  only if the rollout lacks a window.

### Mailbox

Messages are refs addressed to a `fleet` agent ref: sender, body, sent,
read. `scripts/fleet-msg send AGENT TEXT [--urgent]` stores, and with
`--urgent` also pushes over the agent's transport (`codex queue` over ssh,
`SendMessage` locally, tmux paste via `scripts/fleet-remote`). Push only
when the receiver must act before its next natural break (a ruling that
changes current work, an answer it is blocked on). Status, deploy notices
and FYIs are stored only. Claude reads unread mail through a Stop /
PostToolUse hook that appends it to a turn already running; Codex and
Devin read at the handoff points their prompts name (check for a Codex
hook equivalent).

### Harness as todo executor

A todo executor that runs `claude -p` / `codex exec` headless in its own
worktree inside a `sandbox_run` container, scoped token, push to its own
branch only; it reports to the todo and its `fleet` row. Verify first
that `codex exec` keeps code mode (override flags force embedded mode)
and that headless volume is within each plan's terms.

## Steps

1. **Reporter, read-only, no DB.** Prints the fisheye locally on the Mac
   and melchior; run against live sessions for a day to tune state
   detection and exception rules. Verify the capacity sources.
   `scripts/fleet-report` exists (2026-10-09). Verified on the Mac: Claude
   transcript state and usage, Codex `token_count` / `rate_limits` (real
   rollouts carry a weekly `primary`, `secondary: null`). Open: real
   approval-dialog text (`APPROVAL_PATTERNS` are guesses), a live agent
   pane, melchior/Linux, Claude context window per model (assumes 200k,
   `--claude-window`), and `merge-tree` conflict prediction.
2. **`fleet` kind + migration** (`/go`), `test_kind_totality`,
   `test_item_view`, skill `precis-fleet-help`; reporter writes it;
   `scripts/inflight --all-hosts` reads it.
3. **Exception-change wake** of the coordinator; reporter-driven `/clear`.
4. **Mailbox** + `scripts/fleet-msg` + Claude hook delivery.
5. **Round marks:** `scripts/round` dual-writes, with `--for`; `status`
   merges remote marks; flip after a few rounds, `round.json` as fallback.
   Coordinator spawn/close of sessions.
6. **Harness as todo executor.**

Write-path tests on the dev DB. Fallback when prod precis is down: local
files (`round.json`, `.claude/purpose`, `inflight`) keep working.

## Also ruled (2026-10-09): one view, two stores

Pillars, threads and backlog stay in the repo; gripes, todos, quests stay
in precis. The link rot between them gets a **gate check** first: fail
when an item or thread cites a closed or missing gripe/todo id, and list
gripes no item points to. A read-only precis index of repo frontmatter
waits.

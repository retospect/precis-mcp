# Reference — session hooks (`scripts/hooks/`)

Every hook is a small standalone script reading a JSON payload on stdin,
wired in `.claude/settings.json` under `hooks:`. Deny = exit 2 with stderr
(JSON `permissionDecision` for PreToolUse); nudge = stdout context; silence
= pass. Keep each hook single-purpose and never-block-unless-guarding.

## PreToolUse

| Script | Matcher | What it does | Payload fields read |
|---|---|---|---|
| `guard-worktree-path.py` | `Edit\|Write\|MultiEdit\|NotebookEdit` | Rewrites an absolute `file_path` pointing at the MAIN checkout to its worktree twin | `tool_input.file_path`/`notebook_path`, `cwd` |
| `guard-sealed-migration.py` | same | Denies edits to an already-shipped `*.sql` migration (forward-only) | `tool_input.file_path` |
| `rca-triage-nudge.py` | same | One-time nudge toward bug triage on the session's first `src/` edit | `tool_input.file_path`/`notebook_path`, `session_id` |
| `edit-delegation-nudge.py` | `Edit\|Write\|MultiEdit\|NotebookEdit\|Agent\|Task` | Counts main-loop edit calls; past THRESHOLD (50) with no `coder` dispatch, nudges to delegate | `agent_id` (early-return), `session_id`, `tool_name`, `tool_input.subagent_type` |
| `agent-tier-nudge.py` | `Agent\|Task` | Nudges a bare `general-purpose`/`claude` spawn on a top-tier/unset model toward the cheap haiku defs | `tool_input.subagent_type`, `tool_input.model` |
| `guard-secret-read.py` | `Read`, `Bash` | **Denies** wholesale reads of secret-by-convention paths; redacting readers pass. `ALLOW_SECRET_READ=1` escape | `tool_name`, `tool_input.file_path`/`command` |
| `read-dedup.py` | `Read` (Bash payloads reach it via `bash-reflex-nudge.py` delegation) | Refuses to re-serve an unchanged file/range; dedups `cat`/`sed`/`head`/`tail`/`git show` spellings on one ledger | `tool_name`, `tool_input.file_path`/`limit`/`command`, `cwd`, `session_id`, `tool_use_id`, `agent_id`; `source` under `--session-start` |
| `guard-commit-on-main.py` | `Bash` | Denies `git commit` when the target branch is `main`/`master` | `tool_input.command`, `cwd` |
| `guard-git-stash.py` | `Bash` | Denies bare `git stash`/`stash pop` (the stash stack is shared across trees); tagged `push -m` passes | `tool_input.command` |
| `guard-rebase-dirty.py` | `Bash` | Denies `git rebase` on a dirty tree with no autostash in play — the retry-with-`--autostash` path silently rewrites every recorded sha | `tool_input.command`, `cwd` |
| `guard-bare-python-tools.py` | `Bash` | Refuses bare `pytest`/`pip`/`mypy` — wired paths are `scripts/test`, `uv run`, `uv add` | `tool_input.command` |
| `guard-prod-psql.py` | `Bash` | Classifies `scripts/prod-psql` SQL: targeted writes pass, destructive/untargeted ask | `tool_name`, `tool_input.command` |
| `guard-checkout-in-primary.py` | `Bash` | Denies `git checkout`/`switch` of a non-main branch inside the primary checkout | `tool_input.command`, `cwd` |
| `guard-cd-to-primary.py` | `Bash` | Denies a command segment that `cd`s into the primary checkout first | `tool_input.command`, `cwd` |
| `guard-piped-exit-code.py` | `Bash` | Denies piping a status-critical script (`ship`/`deploy`/`bump`) into a filter — the pipeline reports the filter's status | `tool_input.command` |
| `bash-reflex-nudge.py` | `Bash` | Nudges shell-run symbol greps (`rg`/`grep`) toward `scripts/coderef` and inline cluster ops toward delegation; delegates every payload to `read-dedup.py`'s `hook_output` | `tool_name`, `tool_input.command`, `cwd` |
| `coderef-nudge.py` | `Grep` | Nudges a bare-symbol grep toward `scripts/coderef callers|deps` (non-blocking) | `tool_input.pattern`/`glob`/`path`/`type`, `cwd` |
| `guard-prod-write.py` | `mcp__precis__put\|edit\|delete\|tag` | Warns (allows) that a session-MCP write mutates PROD | `tool_name`, `tool_input.kind` |
| `guard-autostash-staged.py` | `Bash` | Denies autostash git verbs (`rebase --autostash`, `pull`, `merge`) while the index is non-empty — the stash pop returns changes UNSTAGED | `tool_input.command`, `cwd` |
| `guard-heredoc.py` | **present, not wired** — the `.claude/settings.json` `Bash` hunk is a pending proposal (gr514124); once wired, `scripts/gen-devin-hooks render` carries it into `.devin/hooks.v1.json` (`exec`) | Denies a `<<` heredoc in a shell command — the one-plain-command rule's repo-side refuse; `<<<` herestrings pass. `ALLOW_HEREDOC=1` escape | `tool_input.command`/`cmd` |

## PostToolUse

| Script | Matcher | What it does | Payload fields read |
|---|---|---|---|
| `map-staleness-reminder.py` | `Write` | On a handler/migration/ADR/skill write, prints a one-line update-the-maps nudge; runs `migration-check` for a new migration | `tool_input.file_path` |
| `session-size-nudge.py` | `*` | Transcript-file-size proxy for context fullness; nudges persist-residuals + propose `/compact`, once per tier | `session_id`, `transcript_path` |

## Lifecycle events

| Script | Event | What it does | Payload fields read |
|---|---|---|---|
| `session-start-lock.sh` | SessionStart | Acquires the `git worktree lock` (`pid <N>`) that marks this tree held by a live session — the liveness signal `scripts/inflight` reads | — |
| `heal-primary-branch.sh` | SessionStart | Heals the PRIMARY checkout back onto `main` when it sits on a fully-merged, clean, unheld branch. `PRECIS_NO_HEAL_PRIMARY=1` | — |
| `session-start-memory.sh` | SessionStart | Prints the one-line memory summary (`precis memory index --for-hook`) once MEMORY.md carries the graph marker; writes the per-node body cache and a last-good copy | — |
| `memory-nudge.sh` | SessionStart | Runs `scripts/memory-lint` in the background (`~/.cache/precis/memory-lint.log`); prints nothing | — |
| `read-dedup.py --session-start` | SessionStart | Deletes the session's dedup state when `source` is `compact`/`clear`/`resume` | `source`, `session_id` |
| `subagent-task-state.py start\|stop` | SubagentStart / SubagentStop | Writes/removes the per-task marker `{agent_id, pid, cwd, started_at}` in the shared git dir; stale markers self-prune | `agent_id`, `cwd` |
| `session-end-reap.sh` | SessionEnd | Releases the worktree lock; reaps THIS session's worktree when `scripts/inflight --json` buckets it `safe_remove`. `reason` `clear`/`resume` passes. `PRECIS_NO_AUTOREAP=1` | `reason`, `cwd` |
| `precompact-persist.sh` | PreCompact | Persist-residuals + hygiene reminder before compaction. Devin-aware: empty stdout there (its payload carries `prompt_id`/`turn_id`; `DEVIN_PROJECT_DIR` is the reliable marker) | `prompt_id`/`turn_id` (runtime detect) |

SessionStart commands wired outside `scripts/hooks/` (same `hooks:` block):
`scripts/mcp-http-install --for-hook` (+ the infrastructure checkout's
`precis-mcp-http-ensure.sh`), `scripts/inflight --for-hook`,
`scripts/reap-worktrees --for-hook`, `scripts/reap-test-dbs --for-hook`,
`scripts/reap-idle-gates --for-hook`, `scripts/reap-stale-serves --for-hook`,
`scripts/main-ci-status --for-hook`, `scripts/docs-index`.

## Not wired in `.claude/settings.json`

- `rtk-hook.sh` — user-level PreToolUse `Bash` shim around `rtk hook claude`
  (wired in `~/.claude/settings.json`): passes search commands and
  git-mentioning commands in a worktree through unrewritten. Reads
  `cwd`, `tool_input.command`, `tool_input.workdir` (Devin fallback).
  → `docs/conventions/rtk.md`
- `codex-efficiency.py` — Codex-harness hook, not Claude: rewrites simple
  read-only commands and counts tool-response bytes per session. Reads
  `cwd`, `session_id`, `hook_event_name`, `tool_input.command`/`cmd`,
  `tool_input.sandbox_permissions`, `tool_response`.
- `compact-state-note.sh` — the compaction state-note hook, user-level:
  a wrapper in `~/.claude/settings.json` (SessionStart `compact` and
  PreCompact) runs `~/.claude/hooks/compact-state-note.sh` when the
  project copy differs or is absent. The project file's only PreCompact
  entry is `precompact-persist.sh`.
- `_symbol_heuristic.py` — not a hook: the shared bare-identifier heuristic
  imported by `coderef-nudge.py` and `bash-reflex-nudge.py`.

## Payload fields

Every PreToolUse/PostToolUse payload (stdin JSON): `tool_name`,
`tool_input` (the call's arguments — Bash uses `command`, file tools
`file_path`, NotebookEdit `notebook_path`), `cwd`, `session_id`,
`tool_use_id`, `transcript_path`, and — for calls made *from inside a
subagent* — `agent_id`. The main loop's payloads carry no `agent_id`; a
subagent's do. That asymmetry is how a hook restricts itself to the main
loop (`edit-delegation-nudge.py` early-returns on a payload that has it;
`subagent-task-state.py` uses it as the task id). Verified against live
payloads in `scripts/hooks/` callers, not the upstream docs — if a new
field appears, record it here.

Other events: SessionStart carries `source`
(`startup`/`resume`/`clear`/`compact`), SessionEnd `reason`, and the Devin
CLI adds `prompt_id`/`turn_id` to every payload (its PreToolUse
`tool_name` is `exec`, with `tool_input.workdir` in place of `cwd`).

Devin's own event names drift across versions: its shipped docs table
(`share/devin/docs/extensibility/hooks/overview.mdx`) lists
`PostCompaction` while the importer's allow-list accepts `PostCompact` /
`PreCompact` (gr515491). Trust the importer, not the docs — the repo
mirror `scripts/lib/devin_hooks.py::DEVIN_EVENTS` is copied verbatim from
the importer's rejection message, and `scripts/gen-devin-hooks check`
(pinned by `tests/test_devin_hooks.py`) keeps `.devin/hooks.v1.json` in
sync with it. To inspect `bin/devin` itself on a Mac without the Xcode
CLT — `strings`/`file` aren't installed there — `grep -a <pattern>
bin/devin` works (gr515492).

**The settings-import WARN has no hooks-only off switch (gr515218).**
Every Devin session start warns that `.claude/settings.json` failed to
parse — expected, and permanent while the file carries a Claude-only
event (`SubagentStart`). The only toggle is `read_config_from.claude`
(`~/.config/devin/config.json` or project `.devin/config.json`), which is
all-or-nothing: `false` silences the WARN but also drops `CLAUDE.md`
rules, `.claude/skills`, `.claude/commands` and the MCP import — not worth
it, since project hooks load from `.devin/hooks.v1.json` regardless
(verified devin 3000.11.205). Treat the WARN as cosmetic; a per-section
import switch is an upstream ask, not a repo config.

## What you cannot verify from the repo

The harness's built-in **worktree-isolation guard** (the one that refuses
compound Bash shapes and `git -C`/`cd` redirects out of the tree — see
`docs/conventions/container-ops.md`) is driver code: none of it — its
rules, ordering, or refusal text — lives in this repo, so no amount of
`rg` proves how it interleaves with the PreToolUse hooks above.

- **Hook-vs-guard ordering** (gr514974): verify from a `claude -w`
  session by sending one command *both* layers refuse —
  `for f in a b; do git stash; done` trips the built-in (git inside a
  construct) and `guard-git-stash.py` (bare `git stash`). The refusal text
  that comes back names the winner: every repo hook's message greps in
  `scripts/hooks/`; the built-in's greps nowhere in the tree.
- **The Devin exec surface is not evidence** (gr514975): Devin's `exec`
  tool does not run the Claude worktree-isolation guard — a
  `for sha in …; do git show …; done` loop that a `claude -w` session
  refuses runs fine under Devin. Guard coverage and refusal text can only
  be probed from a Claude Code session; a green Devin run says nothing
  about the guard. The repo hooks above do still run under Devin (its
  PreToolUse payloads carry `tool_name: "exec"` and
  `tool_input.workdir` — see `rtk-hook.sh`'s header) — only the built-in
  guard is absent.

## Which rules are hook-enforced in which runtime (gr514124)

The footer's "one plain command per Bash call" rules are *documented*
for every runtime but *enforced* unevenly — a builder that assumes the
guard caught a shape only some harnesses refuse is how gr514124/514330
happened:

- **Claude (`claude -w`)** — the built-in worktree-isolation guard
  refuses compound shapes (`<<` heredocs, `&&`/`;`/`||` chains, loops,
  `$(…)`, `cd`) AND the `scripts/hooks/` guards run. Both layers bind.
- **Devin** — no built-in guard; only the repo hooks mirrored into
  `.devin/hooks.v1.json` run. Heredocs, chains and loops therefore *run*
  there even though the documented rule still bans them —
  `guard-heredoc.py` (above) closes the heredoc leg once wired.
- **Codex** — `codex-efficiency.py` is a rewriter/counter, not a deny
  guard; nothing hook-enforces the command-shape rules there at all.

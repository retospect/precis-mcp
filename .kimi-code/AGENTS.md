# precis-mcp — Kimi Code shim

You are Kimi Code working on the precis MCP server. Claude Code sessions
work this repo under `CLAUDE.md`; it binds you too. **Read `CLAUDE.md` in
full before your first edit**, then the repo-root `AGENTS.md` (graph memory,
definition of done). Where they name a Claude-only mechanism, this file
gives the Kimi equivalent.

## Setup (once per machine; `scripts/kimi-tree` re-runs it)

`scripts/kimi-setup` writes `~/.kimi-code/mcp.json` (the `precis` server,
same URL and token as Claude's user-scope entry) and a managed block in
`~/.kimi-code/config.toml`: hooks, `.claude/skills` as a skill dir,
`default_permission_mode = "yolo"` (Claude runs `auto`) and Claude's
allow list as permission rules. `kimi doctor` validates it;
`/mcp` in the TUI shows the connection.

## Mapping from CLAUDE.md

- **Worktree per task.** Start with `scripts/kimi-tree <name>` (not `kimi`
  in the main checkout): it cuts `.claude/worktrees/<name>` from
  origin/main and holds the worktree lock the reaper reads. Never edit or
  commit in the primary `main` checkout.
- **Hooks.** The `.claude/settings.json` guards run under Kimi through
  `scripts/hooks/kimi-adapt.py`. A block's message is the rule; follow its
  `next` step. A worktree-path block hands you corrected arguments —
  re-issue the call with them. A prod-write `ask` block means: ask the user,
  and after they approve re-issue the command prefixed `ALLOW_PROD_WRITE=1 `.
  Never add an `ALLOW_*` override on your own.
- **precis MCP.** Same server as Claude, tools `mcp__precis__get`, `search`,
  `put`, `edit`, `delete`, `tag`, `link`, `more`. It targets PROD: writes
  land in production. Use it the way `CLAUDE.md` says (python/md kinds
  before grep, memory recall at task start, product data through the MCP
  before `scripts/prod-psql`). Discover with
  `search(kind='skill', q='<goal>')`.
- **Skills and slash commands.** `.claude/skills/` (`bug`, `remember`,
  `flow`, ...) and `.claude/commands/*.md` (`land`, `qland`, `go`, `qgo`,
  `next`, ...) both load as Kimi skills; the user types `/skill:qland`
  where Claude takes `/qland`. Command files carry Claude-only syntax:
  a `` !`cmd` `` line is live state Claude injects — run `cmd` yourself;
  `$ARGUMENTS` is whatever the user typed after the command. Steps
  that name a Claude tool (`Agent` subagent types from `.claude/agents/`,
  `SendMessage`, `EnterWorktree`) — do that step yourself, or use Kimi's
  `Agent` tool with the remit text from the named `.claude/agents/<name>.md`.
- **Bash.** One plain command per call, same as Claude. There is no rtk
  rewrite hook here: prefix noisy commands (`git log`, `ls -R`, big greps)
  with `rtk` yourself. Never pipe `scripts/ship|deploy|bump` into a filter.
- **Commits.** `type(scope): what changed` (≤72 chars), message via
  `git commit -F scratch/commit-msg.txt`, ending with:

      Co-Authored-By: Kimi Code (<model display name>) <noreply@moonshot.ai>
      Claude-Session: none (Kimi Code builder, <tree name>)

- **Fleet.** In a fleet session, a question for Reto is an `ask-user`
  todo with `meta.ask` (`search(kind='todo', view='asks')`; skill
  `precis-work-help`); leave deploys and `/go` to the coordinator.
- **Response style.** `CLAUDE.md` §Response style applies unchanged.

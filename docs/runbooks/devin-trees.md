# Devin CLI task trees

Devin CLI on the Mac takes repo work in its own worktree, like `claude -w`.
It reads `CLAUDE.md` and `AGENTS.md` as always-on rules and runs the
`.devin/workflows/` commands (go, land, whatneedsdoing).

## Launch

```
scripts/devin-tree NAME --purpose "one line" [--prompt-file FILE] \
  [--model swe-2-medium] [--session TMUX_SESSION]
```

It creates `.claude/worktrees/NAME` on branch `worktree-NAME` from
`origin/main`, or reuses an existing tree or branch without resetting it. It
writes `.claude/purpose` and opens tmux window `devin-NAME` running
`devin --permission-mode auto`. The tree is locked `pid <devin pid>`, so
`scripts/inflight` shows it live and the reaper leaves it alone until Devin
exits. A merged, clean tree then reaps once its purpose is over 6 hours old.
Outside tmux, pass `--session`. `--dry-run` prints the plan.

## Precis access

Devin reads its MCP config from `~/.config/devin/mcp_config.json` (user
scope, chmod 600): server `precis`, HTTP `http://127.0.0.1:8765/mcp`, with the
same bearer header as `~/.claude.json`. That server is PROD. The first call to
each tool asks for permission: allow `get`/`search`/`more` for read-only work.
Never choose "all tools" unless the task writes to precis.

## Models

`devin models list` shows the ids. Every SWE-2 tier (`swe-2-medium`,
`swe-2-high`, `swe-2-max`) is free; the default is SWE-2 High.

# Devin CLI task trees

Devin CLI on the Mac takes repo work in its own worktree, like `claude -w`.
It reads `CLAUDE.md` and `AGENTS.md` as always-on rules and runs the
`.devin/workflows/` commands (go, land, whatneedsdoing).

## Launch

The common entry is `scripts/agent-start devin NAME`; see
[agent-start](agent-start.md) for the shared checkout and lock contract.
The existing entry below remains supported.

```
scripts/devin-tree NAME --purpose "one line" [--prompt-file FILE] \
  [--model swe-2-medium] [--tmux [SESSION]]
```

It creates `.claude/worktrees/NAME` on branch `worktree-NAME` from
`origin/main`, or reuses an existing tree or branch without resetting it. It
writes `.claude/purpose` and runs `devin --permission-mode dangerous` in
this terminal; with `--tmux [SESSION]` it opens tmux window `devin-NAME`
instead (`--session SESSION` is the old spelling; `scripts/fleet-wave` passes
`--tmux`). Before launch it adds the repo's
`.claude/worktrees` directory to `trusted_paths` in
`~/.local/share/devin/cli/trusted_workspaces.json` — Devin trusts
subfolders of a listed parent, so the one entry covers every tree and
Devin never stops at the directory-trust prompt. The tree is locked
`pid <devin pid>`, so
`scripts/inflight` shows it live and the reaper leaves it alone until Devin
exits. A merged, clean tree then reaps once its purpose is over 6 hours old.
`--tmux` outside tmux needs a SESSION. `--dry-run` prints the plan.

A Claude session starts Devin only through `scripts/devin-tree` or
`scripts/fleet-wave launch NAME --devin`. Both pass `--permission-mode
dangerous` under Reto's ruling me499092. Do not type `devin
--permission-mode dangerous` yourself: Claude's auto-mode classifier denies
the hand-typed command.

`ln -s <main checkout>/scripts/devin-tree ~/.local/bin/devin-wt` gives the
same entry point as `kimi-wt` (`scripts/kimi-tree`): `devin-wt NAME` runs
Devin in the current terminal.

## Precis access

Devin's tool prefix is the server key, and the key may be `precis-devin`,
not `precis`. List the keys before the first call: `devin mcp list` prints
each key with its URL and source file, and no token. Do not open
`~/.config/devin/mcp_config.json` for it; that file holds the bearer token.

On the dev host (2026-11-22) a tree lists two keys, both at
`http://127.0.0.1:8765/mcp`, which is PROD:

| key | source | in a launched tree |
|---|---|---|
| `precis-devin` | `~/.config/devin/mcp_config.json` (user scope, chmod 600) | answers; call this one |
| `precis` | the tree's `.mcp.json` | `list_tools` failed in every tree checked; do not retry it |

A tree launched by `scripts/devin-tree` or `scripts/fleet-wave` runs
`--permission-mode dangerous`, so nothing prompts. Under `auto`,
`accept-edits` or `smart` a call can be refused, and the tool result is
only `User rejected this tool call`. That is a permission denial: do not
retry the identical call; use another route or report it. A wave builder
reports it to its coordinator and does not get the same effect another way
(the footer's refusal rule). Stick to `get`/`search`/`more` for read-only
work and never write unless the task writes to precis.

## Non-tty shell: prefix editor-spawning git with `GIT_EDITOR=true`

Devin's shell is not a tty. A git command that opens an editor —
`git rebase --continue` after a resolved conflict, `git commit
--amend`, a merge — spawns vim on non-tty stdin and hangs, and
`write_to_process` cannot send `:wq` to it ("Failed to write to
shell"), so a hung editor means killing the shell (gr515810, gr515813).
Prefix the command with `GIT_EDITOR=true` to accept the recorded
message non-interactively — `GIT_EDITOR=true git rebase --continue` is
the standard conflict-resolution step (the rebase recipe in
`docs/conventions/shipping.md` §Sync, gr514728).

## Models

`devin models list` shows the ids. Every SWE-2 tier (`swe-2-medium`,
`swe-2-high`, `swe-2-max`) is free; the default is SWE-2 High.

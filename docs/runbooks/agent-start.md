# Start an agent in a task checkout

From the repository, run:

```sh
scripts/agent-start
scripts/agent-start codex newpapers
scripts/agent-start claude newpapers
scripts/agent-start kimi newpapers
scripts/agent-start devin newpapers
```

With no arguments, `agent-start` lists the supported agents and their installed
executable locations, plus launch usage. `--list`, `--help` and `-h` show the
same information. Listing performs no worktree creation or agent launch;
agent-specific setup checks still run when starting a session.

`newpapers` is the task name: checkout `.claude/worktrees/newpapers`, branch
`worktree-newpapers`. A new branch starts at `origin/main`; an existing
branch or checkout is reused without resetting changes. Only one live
session can own the checkout; use another name for parallel work.

Common options are `--purpose TEXT`, `--model MODEL`, `--tmux [SESSION]`
and `--dry-run`. With no model option, the agent's own selection remains
in effect. `--tmux` needs a session name when called outside tmux.

Claude and Codex accept native arguments after `--`, including the initial
prompt:

```sh
scripts/agent-start codex newpapers -- -- "Review the new papers"
scripts/agent-start claude parser-fix --model sonnet -- "Review the parser"
```

The first `--` ends launcher options. Native options that relocate the
checkout (`-w`, `--worktree`, `-C`, `--cd`, `--remote`) are refused. Kimi
and Devin delegate to `scripts/kimi-tree` and `scripts/devin-tree`, retaining
their existing options, setup checks and permission defaults. Kimi remains
interactive; its launcher refuses headless prompt automation. No launcher
changes the user's model or integration configuration.

All four use `scripts/lib/agent-tree.sh::at_ensure_tree`: registration,
repository, branch, non-sparse checkout and on-disk tracked files are checked
before launch. A mismatched or empty directory requires explicit recovery.
Creation acquires a PID lock before checkout; the lock protects environment
seeding and is handed to the agent process through terminal or tmux launch.
Another live session's lock is never replaced by a normal launch.

`scripts/claude-tree NAME` and `scripts/codex-tree NAME` are equivalent
entry points. Existing `kimi-wt` and `devin-wt` symlinks keep working.
The managed `scripts/agent-cli-update --host NAME --apply` path installs or
repairs `~/.local/bin/agent-start` on the target account. It discovers the
stable checkout from existing `kimi-wt`, `devin-wt` or `agent-start` symlinks;
set `agent_cli_launcher_checkout` in inventory when those hints are missing
or disagree. The source must contain committed, unmodified launcher code in
a primary checkout. Task-worktree sources and user-file conflicts are
refused. Report mode and Ansible check mode create no links. Once installed,
the symlink follows subsequent shipped updates in that checkout.

For an unmanaged installation, the equivalent is:

```sh
ln -s /path/to/precis-mcp/scripts/agent-start ~/.local/bin/agent-start
```

The checkout verification applies to this launcher; it does not repair
unrelated sessions started through native `codex --worktree` or `claude -w`.

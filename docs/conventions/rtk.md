# Convention — `rtk` (token-killer CLI proxy)

**What it is.** `rtk` is a Rust CLI proxy that compresses noisy command
output before it reaches the model's context — a prerequisite tool on
Reto's dev Mac, same tier as `uv`/`docker` (`brew install rtk`).

## How it gets invoked

A **global PreToolUse hook** (`rtk init --global`) is installed once on
Reto's dev Mac, so it covers *every* local worktree session automatically —
no manual prefix needed. The hook rewrites a Bash command to `rtk <cmd>`
transparently before it runs.

Only rtk's *known* commands are rewritten (git/psql/grep/find/docker/cargo/
pytest…); `scripts/*` wrappers and the already-terse `scripts/test` pass
through untouched. The repo's Bash guards (commit-on-main / git-stash /
prod-psql) are prefix-robust, so the rewrite can't blind them.

## Consequence: filtered, not raw, output

Because the hook silently rewrites the command, **Bash tool output is a
filtered digest, not the raw stream.** If a detail you need is missing:

- `rtk proxy <cmd>` — raw passthrough of the same command, no filtering.
- Or read the teed full log (rtk keeps the unfiltered output alongside the
  digest).

## No hook outside Reto's dev Mac

CI and cluster `claude -p` invocations don't have the PreToolUse hook
installed, so nothing auto-rewrites there — prefix manually:

```
rtk git …
rtk err -- <cmd>       # just the error signal
rtk summary -- <cmd>   # condensed summary
```

## Filters and uninstall

- Filters live in a committed `.rtk/filters.toml`, which overrides the
  user-global template — repo-specific noise (a chatty test runner, a
  verbose migration tool) gets its own rule there.
- `rtk gain` / `rtk gain --history` — token-savings analytics.
- `rtk discover` — mines Claude Code history for missed opportunities
  (commands that should have been rewritten but weren't).
- Uninstall the hook: `rtk init --global --uninstall` (then restart the
  session — the hook is read once at session start).

---
name: quiet-output
description: >-
  Before running a verbose dev command whose output would flood the session —
  a deploy (scripts/deploy), an image build, an ingest run, a migration, a
  git/gh/psql/grep/find with a large result — run it through `rtk` so only the
  signal reaches context instead of a 1000-line firehose. Repo-dev tool for
  developing precis-mcp; NOT a precis product skill.
---

# quiet-output — keep noisy command logs out of context with rtk

Verbose command output is the biggest avoidable context sink in this repo's
dev loop. `rtk` (a token-killer CLI proxy; `brew install rtk`) filters a
command's output to just the signal — failures, compacted tables, grouped
errors — and tees the full log to disk. We use it **manually, no auto-hook**:
the explicit `rtk` in the command line is itself the signal that a filter is in
play, so filtered output is never mistaken for raw.

## When to reach for it

Prefix the noisy command:

    rtk err -- scripts/deploy          # arbitrary command, errors/warnings only
    rtk summary -- <cmd>               # arbitrary command, heuristic summary
    rtk git status | rtk git diff      # compact git
    rtk psql -- <psql args>            # borderless, compressed tables
    rtk grep / rtk rg / rtk find       # compact search output

`rtk` is a safe passthrough — a subcommand it doesn't specially handle runs
unchanged. Project-specific filters live in `.rtk/filters.toml` (committed).

## When NOT to

- **Tests** — `scripts/test` already terse-ifies pytest; don't double-wrap.
- **Interactive** commands (a shell, an interactive `psql`, anything needing a
  TTY) — rtk captures output, so there's nothing to type into.
- When you genuinely need the raw stream — run it without `rtk`.

## The output is a DIGEST, not raw

Treat rtk output as a filtered digest. If a detail you need was compressed
away, **re-run the command raw** (or read the tee'd full log rtk prints) —
never assume the digest is the whole story.

**Exit codes:** `rtk err -- <cmd>` propagates the child's exit code; `rtk
summary -- <cmd>` does NOT (it returns 0 even on failure). So when the exit
status matters — chaining on `&&`, gating on success — use `rtk err`, not `rtk
summary`.

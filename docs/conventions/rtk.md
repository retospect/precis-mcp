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

## Don't hand-suppress stderr (`2>/dev/null`)

Under rtk, a blanket `2>/dev/null` is net-negative. rtk's model is *drop
the noise, keep the error signal* (`rtk err` extracts exactly that), but
`2>/dev/null` discards stderr **before rtk sees it** — so a failure returns
silence instead of a digested error, and you burn a round trip re-running
without the redirect. You pay the cost of blinding yourself to failures to
solve a noise problem rtk already solves. It's also risky in compound shapes:
`cd X 2>/dev/null; cmd` is the exact pattern `guard-cd-to-primary.py` had to
be widened to catch — the redirect gets absorbed into the path and smuggles
past the guard.

So: run bare and let rtk filter. For a genuinely chatty tool, add a targeted
rule to `.rtk/filters.toml` (shapes that command's output, keeps errors) —
not a redirect that also swallows the failure. Reach for `2>/dev/null` only
when the silence is *control-flow logic* rather than noise reduction — e.g. a
quiet probe like `command -v foo 2>/dev/null || echo missing`, where you want
silence regardless of outcome and branch on the exit code.

## No hook outside Reto's dev Mac

CI and cluster `claude -p` invocations don't have the PreToolUse hook
installed, so nothing auto-rewrites there — prefix manually:

```
rtk git …
rtk err -- <cmd>       # just the error signal
rtk summary -- <cmd>   # condensed summary
```

## Known gaps

- **`find` with a compound predicate.** The global hook rewrites *any*
  `find …` invocation to `rtk find …` before it runs — the rewrite step
  doesn't inspect arguments. But `rtk find` itself then refuses `-not`,
  `-exec`, and likely other compound predicates/actions ("rtk find does not
  support compound predicates or actions … Use find directly"), so the
  first attempt always burns a round trip. Skip the retry: if your `find`
  needs `-not`, `-exec`, `-prune` combined with an action, etc., write it as
  `rtk proxy find …` up front — same command, raw passthrough, still
  tracked, no rejection. (Confirmed against rtk 0.43.0; `rtk rewrite
  "find … -not …"` happily emits the doomed `rtk find …` rewrite, so this
  is a real upstream gap in the rewrite/subcommand split, not something
  `.rtk/filters.toml` can patch — that file only shapes *output* filtering
  for commands rtk already decided to run, not the pre-execution
  rewrite-vs-passthrough decision.)

## Filters and uninstall

- Filters live in a committed `.rtk/filters.toml`, which overrides the
  user-global template — repo-specific noise (a chatty test runner, a
  verbose migration tool) gets its own rule there.
- `rtk gain` / `rtk gain --history` — token-savings analytics.
- `rtk discover` — mines Claude Code history for missed opportunities
  (commands that should have been rewritten but weren't).
- Uninstall the hook: `rtk init --global --uninstall` (then restart the
  session — the hook is read once at session start).

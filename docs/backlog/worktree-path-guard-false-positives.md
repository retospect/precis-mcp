---
status: idea
title: worktree guard — upstream asks (rtk git refusal, fail-closed dynamic payloads)
pillar: platform
---

# Worktree guard — upstream asks

The harness's worktree-isolation guard is a **built-in, not a repo hook** —
the refusal text appears nowhere in `scripts/hooks/`, `scripts/` or
`.claude/`; `guard-worktree-path.py` only auto-corrects Edit/Write paths.
Nothing here is tunable from this repo; what remains is flagging two
mechanisms upstream (gr456287).

Measured (surface-review pass #1, 2026-09-29): **308 refusals across 97 of
99 sessions** in a 5-day window — 95 generic "too complex", 68
git-in-a-construct, 34 `rtk`, 18 `git -C`, 88 other. Two mechanisms
isolated by live probing:

1. **`rtk git ...` refused unconditionally**, even bare — contradicts
   `.claude/settings.json`'s own `Bash(rtk git:*)` allow-list and the
   CLAUDE.md-documented `rtk` pattern.
2. **Interpreter launchers fail closed on dynamic program text regardless
   of content** — `X=1; python3 -c "print($X)"` is refused with zero git in
   the command; relevance cannot be checked before execution, so it
   defaults to refuse. This is most of the 95-generic + 88-other buckets.
   The static matcher is already precise (word-boundary: `github`,
   `git_sha`, `legitimate` all pass), so the gap is specifically the
   dynamic-payload branch.

And the asymmetry that argues for loosening rather than a tuning pass:
fail-closed is not buying real safety — a throwaway script that shells out
internally sails through (the guard matches command *text*; one level of
indirection defeats it), so the rule taxes benign heredocs while a
motivated bypass is one indirection away.

Repo-side half is done and lives in
`docs/conventions/container-ops.md` §"Refused shapes and the accepted
alternative" (cheat sheet, one-plain-command shape, script-file pattern)
and `scripts/inflight --json` for cross-worktree reads.

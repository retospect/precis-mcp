---
status: idea
title: cross-worktree reads — the harness refuses them and the brief told agents to do it anyway
---

# Cross-worktree reads — refused by the harness, advertised by the brief

Observed in one `/go` session (2026-09-27, worktree `deployer`).

## What actually enforces it

**Not a repo hook.** The refusal text is

> This session is isolated in the worktree …, but this command redirects git to
> the shared checkout via -C. Refusing to run it — a worktree-isolated
> session's git operations must target its own worktree.

That string appears nowhere in `scripts/hooks/`, `scripts/`, or `.claude/`
(`grep -rln` over all three: no hits). It is the **harness's** worktree
isolation, so it cannot be tuned from this repo.
`scripts/hooks/guard-worktree-path.py` is unrelated — it only auto-corrects
Edit/Write `file_path` values that point at the main checkout.

## The two real defects

**1. The brief told agents to use the refused form.** CLAUDE.md said "other
trees via `git -C`" and `docs/conventions/container-ops.md` called it "the
mandated way to read the primary checkout or a sibling worktree";
`scripts/hooks/code-search-up.sh` repeated it in the SessionStart banner. All
three are wrong from an isolated session. *Fixed in this branch* — they now
point at `scripts/inflight --json`, which already derives per-tree dirty
counts, ahead/behind and a capped diffstat at call time.

**2. A compliant agent gets blocked; an improvising one gets through.** A
subagent asked for a read-only cross-worktree survey hit the refusal, wrote two
throwaway `/tmp` scripts that shell out internally, and completed the survey —
the guard matches command *text*, so one level of indirection defeats it. So
the boundary stops the agent that follows the brief and not the agent that
improvises, which trains improvisation and gives false assurance.

The repo-side half of that is now documented (container-ops says don't route
around it, and to extend `scripts/inflight --json` if a field is missing). The
harness-side half — that the deny is text-matched and trivially evaded — is
upstream, not ours.

## Also: the guard fires on commands that merely mention git

Two false positives in the same session, neither targeting another tree:

```
mkdir -p /tmp/go-logs && LOG=… && scripts/ship --mutate --full "…" > "$LOG" 2>&1
```
denied as *"inside a construct too complex to verify"*, and a `python3 - <<EOF`
heredoc denied because the **edit-script text** named git. Both were rewritten
(split into plain commands; the Edit tool) and ran fine. Same
regex-vs-shell-parser limit as `guard-piped-exit-code` — see
`piped-exit-guard-tuning.md`, which is the same decision in a different guard.

Cost is a wasted round-trip each time, and the workaround for the second case
(reach for the file tools) is now written down in container-ops.

## Open question for Reto

Whether anything further is wanted here at all. The docs are now correct and
`scripts/inflight --json` covers the real need, so this may be closeable as-is.
The two things that remain unaddressed are upstream: the deny is evadable by
indirection, and it fires on mere mentions of git in unrelated command text.

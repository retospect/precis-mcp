---
status: idea
title: cross-worktree reads — the harness refuses them and the brief told agents to do it anyway
pillar: platform
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

## 2026-09-29 — measured; NOT closeable as-is (gr456287)

surface-review pass #1 put a number on it: **308 refusals across 97 of 99
sessions** in a 5-day window — the highest-frequency friction signature in the
entire corpus. Reto: *"we wanna fix that."* So the open question above is
answered — this is not closeable.

Breakdown: 95 generic "too complex", 68 git-in-a-construct, 34 `rtk`, 18
`git -C`, 3 prod-psql, 88 other (gh, ship, deploy, docker, heredocs,
arithmetic). Two mechanisms isolated by live probing, both new since this item
was written:

1. **`rtk git ...` is refused unconditionally**, even bare with no construct —
   which contradicts `.claude/settings.json`'s own `Bash(rtk git:*)` allow-list
   and the `rtk` pattern CLAUDE.md tells agents to use. The product and the
   harness disagree, and the agent pays.
2. **Interpreter launchers fail closed on dynamic program text regardless of
   content.** `X=1; python3 -c "print($X)"` is refused with *zero* git anywhere:
   relevance cannot be checked before execution, so it defaults to refuse. This
   is the source of the 95 generic + much of the 88 "other" bucket.

The static case is already precise (word-boundary matched — `github`,
`git_sha`, `legitimate` all pass), so the gap is specifically the
dynamic-payload branch.

Worth stating plainly before anyone "fixes" the dynamic branch: per the
indirection note above, fail-closed is **not** buying real safety — a
throwaway `/tmp` script that shells out internally already sails through. The
current rule taxes benign heredocs while a motivated bypass is one level of
indirection away. That asymmetry, not the false-positive count alone, is the
argument for loosening it.

In-repo levers are limited (the guard is a harness built-in, re-confirmed:
nothing in `scripts/hooks/` or `.claude/settings.json` emits this text):
put the accepted-shape cheat sheet in `docs/conventions/container-ops.md`, and
flag the two mechanisms upstream.

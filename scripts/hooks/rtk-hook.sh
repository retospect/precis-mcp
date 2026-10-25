#!/bin/sh
# Shim around `rtk hook claude` for the user-level Claude Code PreToolUse hook
# (matcher Bash). Wire it in ~/.claude/settings.json as:
#   S="$CLAUDE_PROJECT_DIR/scripts/hooks/rtk-hook.sh"; [ -f "$S" ] && sh "$S" || rtk hook claude
#
# Why: in a worktree-isolated session (`claude -w`), Claude Code's built-in
# isolation guard refuses any rtk-wrapped command that carries a `git` token
# ("runs rtk with a git command among its operands ... cannot be shown not to
# be git"). rtk rewrites `git status` to `rtk git status`, so every direct
# git call, and any grep/find whose arguments mention git, is refused. Seen
# on Claude Code 2.1.267 + rtk 0.49.0 (docs/conventions/rtk.md, Known gaps).
#
# What: one deny, then two pass-throughs that emit nothing (= leave the
# command unrewritten).
#  0. `rg` carrying a short-flag cluster that contains r (`rg -rn`,
#     `rg -rln`, `rg -nr`, `rg -lnr`): in ripgrep -r is --replace <text>, so
#     -rn rewrites every match to 'n' and -nr swallows the pattern as the
#     replacement text (gr515812; Reto ruling "block rn", extended to any
#     cluster 2026-10-25). Emits the same PreToolUse deny JSON as the
#     guard-* hooks, before either passthrough or the rtk delegation can
#     run — `rtk rg -rn` included. Only a bare `-r` followed by a quoted
#     argument (`rg -r 'x'`, `rg -r "x"`) and `--replace`/`--replace=` stay
#     allowed; `rg -r x` and `-r'x'` are denied too (the grep -r idiom).
#  1. Search commands — `rg`, `*grep` (grep/egrep/fgrep/zgrep/…, which also
#     covers `git grep`) — in ANY session. rtk's digest rewrites matched
#     identifiers and collapses match sets (gr514332, gr513881, gr514780;
#     Reto ruling 2026-10-24): the exact lines a search returns must reach
#     the model verbatim, so these never go to rtk at all.
#  2. When the session cwd is under .claude/worktrees/ and the command
#     mentions `git` as a word: the isolation guard still vets the plain
#     command; only rtk's digest is lost.
# Everything else goes to rtk as before.
#
# Payload shapes: Claude Code sends {"cwd": …, "tool_input": {"command": …}};
# Devin CLI sends a PreToolUse payload with tool_name "exec", no top-level
# "cwd" — the session dir is the hook process's own $PWD (DEVIN_PROJECT_DIR is
# also exported to hook commands). Fall back to tool_input.workdir, then $PWD,
# so the worktree passthrough still fires when Devin runs this script.
input=$(cat)
fields=$(printf '%s' "$input" | python3 -c '
import json, os, re, shlex, sys
d = json.load(sys.stdin)
ti = d.get("tool_input", {})
cmd = ti.get("command", "")

PUNCT = set("|&;()")
PREFIXES = {"rtk", "env", "command", "proxy"}
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
Q = (chr(39), chr(34))  # single and double quote chars

def unquote(tok):
    if len(tok) >= 2 and tok[0] in Q and tok[-1] == tok[0]:
        return tok[1:-1]
    return tok

def rg_replace_cluster(command):
    # gr515812: rg -r is --replace <text>; any short-flag cluster containing
    # r mangles (-rn rewrites every match to 'n', -nr swallows the pattern
    # as the replacement). Only a bare -r followed by a quoted argument and
    # --replace/--replace= stay allowed — rg -r x is the grep -r idiom and
    # is denied. rg as a command word only: command start, after a
    # separator/subshell, or behind an env/rtk/assignment prefix.
    try:
        toks = list(shlex.shlex(command, posix=False, punctuation_chars="|&;()"))
    except ValueError:
        return False
    i, expect_cmd, n = 0, True, len(toks)
    while i < n:
        if set(toks[i]) <= PUNCT:
            expect_cmd = True
            i += 1
            continue
        if expect_cmd:
            j = i
            while j < n and (toks[j] in PREFIXES or ASSIGN.match(toks[j])):
                j += 1
            if j < n and unquote(toks[j]) == "rg":
                k = j + 1
                while k < n:
                    tok = toks[k]
                    if set(tok) <= PUNCT or tok == "--":
                        break
                    flag = unquote(tok)
                    if flag.startswith("--"):
                        k += 1
                        continue
                    if flag == "-r":
                        if k + 1 < n and toks[k + 1][:1] in Q:
                            k += 2
                            continue
                        return True
                    if flag.startswith("-") and "r" in flag[1:]:
                        return True
                    k += 1
            expect_cmd = False
        i += 1
    return False

print("deny" if rg_replace_cluster(cmd) else "")
print(d.get("cwd") or ti.get("workdir") or os.environ.get("PWD", ""))
print(cmd)
' 2>/dev/null)
verdict=$(printf '%s\n' "$fields" | sed -n 1p)
cwd=$(printf '%s\n' "$fields" | sed -n 2p)
cmd=$(printf '%s\n' "$fields" | sed -n '3,$p')
if [ "$verdict" = "deny" ]; then
  printf '%s\n' "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"deny\",\"permissionDecisionReason\":\"rg -r is --replace <text>; -rn rewrites every match to 'n'. Use rg -n (gr515812).\"}}"
  exit 0
fi
if printf '%s' "$cmd" | grep -qE '(^|[^[:alnum:]_])(rg|[a-z]*grep)([^[:alnum:]_]|$)'; then
  exit 0
fi
case "$cwd" in
  */.claude/worktrees/*)
    if printf '%s' "$cmd" | grep -qE '(^|[^[:alnum:]_])git([^[:alnum:]_]|$)'; then
      exit 0
    fi
    ;;
esac
printf '%s' "$input" | exec rtk hook claude

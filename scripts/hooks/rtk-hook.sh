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
# What: two pass-throughs emit nothing (= leave the command unrewritten).
#  1. Search commands — `rg`, `*grep` (grep/egrep/fgrep/zgrep/…, which also
#     covers `git grep`) — in ANY session. rtk's digest rewrites matched
#     identifiers and collapses match sets (gr514332, gr513881, gr514780;
#     Reto ruling 2026-10-24): the exact lines a search returns must reach
#     the model verbatim, so these never go to rtk at all.
#  2. When the session cwd is under .claude/worktrees/ and the command
#     mentions `git` as a word: the isolation guard still vets the plain
#     command; only rtk's digest is lost.
# Everything else goes to rtk as before.
input=$(cat)
fields=$(printf '%s' "$input" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(d.get("cwd", ""))
print(d.get("tool_input", {}).get("command", ""))
' 2>/dev/null)
cwd=$(printf '%s\n' "$fields" | sed -n 1p)
cmd=$(printf '%s\n' "$fields" | sed -n '2,$p')
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

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
# What: when the session cwd is under .claude/worktrees/ and the command
# mentions `git` as a word, emit nothing (= leave the command unrewritten).
# Everything else goes to rtk as before. The only cost is rtk's digest on
# those commands; the isolation guard still vets the plain command.
input=$(cat)
fields=$(printf '%s' "$input" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(d.get("cwd", ""))
print(d.get("tool_input", {}).get("command", ""))
' 2>/dev/null)
cwd=$(printf '%s\n' "$fields" | sed -n 1p)
cmd=$(printf '%s\n' "$fields" | sed -n '2,$p')
case "$cwd" in
  */.claude/worktrees/*)
    if printf '%s' "$cmd" | grep -qE '(^|[^[:alnum:]_])git([^[:alnum:]_]|$)'; then
      exit 0
    fi
    ;;
esac
printf '%s' "$input" | exec rtk hook claude

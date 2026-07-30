#!/usr/bin/env bash
# SessionStart hook: acquire the `git worktree lock` that marks THIS
# worktree as held by a live Claude session.
#
# scripts/inflight's session_field() (and therefore scripts/reap-worktrees /
# scripts/hooks/session-end-reap.sh, which never reimplement liveness) reads
# liveness ONLY from a worktree's lock reason: `pid <N>` + `kill -0 <N>`.
# Without something acquiring that lock, every worktree shows `session = —`
# and looks reapable the moment scripts/ship leaves it merged + clean — even
# while a session is still sitting in it. This is the acquire half; the
# release half is scripts/hooks/session-end-reap.sh's `worktree unlock`.
#
# The PID recorded is NOT this hook script's own pid (a short-lived `bash`
# child that exits immediately) — it's the long-lived `claude` / `claude -w`
# process itself, found by walking the parent chain up from $PPID. That's
# the pid that's alive for the session's whole duration and dies when the
# session truly ends, which is exactly what `kill -0` needs to check.
#
# Identifying that process by a `*claude*` SUBSTRING on the full command
# line is unsound and was rejected once already (gr172130): every worktree's
# own path is `.claude/worktrees/<name>/...`, so a short-lived intermediate
# wrapper (a shell-snapshot `sh -c "..."`, etc.) invoked with that path
# somewhere in its argv also matches `*claude*` — and if the walk hits that
# wrapper before the real session process, it locks the worktree to a pid
# that's already gone by the time anything checks `kill -0`, producing an
# immediate `dead-lock#<pid>` that a sibling session's reaper then deletes
# out from under the still-live session. Exactly the bug this hook exists to
# fix. See `find_session_pid` below for the discriminator used instead.
#
# No-op for the PRIMARY checkout (only worktrees under .claude/worktrees/
# are ever locked) and idempotent (unlock-then-lock), so a restarted/resumed
# session just refreshes its own lock instead of erroring on "already locked".
#
# Escape hatch: PRECIS_NO_AUTOREAP=1 → no-op (matches reap-worktrees /
# session-end-reap.sh — no point locking for a liveness check nothing acts on).
# Wired in .claude/settings.json (SessionStart), before scripts/inflight.
set -uo pipefail

[ -n "${PRECIS_NO_AUTOREAP:-}" ] && exit 0

cd "$(dirname "$0")/../.." || exit 0
command -v git >/dev/null 2>&1 || exit 0
git rev-parse --git-dir >/dev/null 2>&1 || exit 0

HERE=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
PRIMARY=$(git worktree list --porcelain 2>/dev/null | awk '/^worktree /{print $2; exit}')
[ -z "$PRIMARY" ] && exit 0
[ "$HERE" = "$PRIMARY" ] && exit 0   # never lock the primary checkout

case "$HERE" in
    */.claude/worktrees/*) ;;
    *) exit 0 ;;   # not the recognized worktree layout — be conservative
esac

# Walk the parent chain from $PPID (the hook script's own parent) up to the
# durable `claude` / `claude -w <name>` process this session lives and dies
# with. Bounded (25 hops) so a broken /proc or a ps failure can't spin.
#
# Discriminator, in order:
#
#   1. `comm` (the process's own executable basename — e.g. "claude",
#      "bash", "node" — NOT the full command line with its args/paths)
#      is exactly "claude". This is what the native/binary Claude Code
#      install (e.g. Homebrew's /opt/homebrew/bin/claude) presents as, and
#      it can't be spoofed by a path substring: `comm` never contains a
#      worktree's own ".claude/worktrees/<name>" path, only the exec name.
#
#   2. Fallback for an npm/node-shim install, where the OS-level process is
#      literally "node" (the `claude` shebang script re-execs it) and #1
#      never matches: check that same process's full argv for the npm
#      package path "@anthropic-ai/claude-code". That's a fixed install-path
#      token — far more specific than a bare "claude" substring — that would
#      never coincidentally appear in this repo's own worktree paths (which
#      live under .claude/worktrees/<name>, nowhere near a node_modules
#      package path).
#
# Either way this only matches the process itself, never a transient
# wrapper that merely mentions "claude"/the repo path somewhere in argv.
find_session_pid() {
    local pid=$1 i comm base args ppid
    for i in $(seq 1 25); do
        [ -z "${pid:-}" ] && return 1
        case "$pid" in ''|*[!0-9]*) return 1 ;; esac
        [ "$pid" -le 1 ] && return 1

        comm=$(ps -o comm= -p "$pid" 2>/dev/null)
        base=${comm##*/}
        if [ "$base" = "claude" ]; then
            printf '%s' "$pid"
            return 0
        fi
        if [ "$base" = "node" ]; then
            args=$(ps -o args= -p "$pid" 2>/dev/null)
            case "$args" in
                *"@anthropic-ai/claude-code"*)
                    printf '%s' "$pid"
                    return 0
                    ;;
            esac
        fi

        ppid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d '[:space:]')
        [ -z "$ppid" ] && return 1
        pid=$ppid
    done
    return 1
}

SESSION_PID=$(find_session_pid "${PPID:-}") || exit 0
[ -z "$SESSION_PID" ] && exit 0

# The pid must actually be alive right now — belt-and-suspenders against the
# (tiny) race between the ps snapshot above and locking below.
kill -0 "$SESSION_PID" 2>/dev/null || exit 0

# Idempotent: drop any prior lock (stale reason, or a resumed session
# refreshing its own) before re-acquiring with the current pid.
git worktree unlock "$HERE" >/dev/null 2>&1 || true
git worktree lock "$HERE" --reason "pid $SESSION_PID" >/dev/null 2>&1 || true
exit 0

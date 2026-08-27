# scripts/lib/session-lock.sh — shared helpers for identifying the durable
# Claude session process behind a hook invocation, and for reading/comparing
# against whatever pid a worktree's `git worktree lock` currently names.
# Sourced (not executed) by scripts/hooks/session-start-lock.sh and
# scripts/hooks/session-end-reap.sh — never reimplemented per-hook, so the two
# hooks' notion of "whose session is this" can't drift apart.
#
# gr256469: a nested `claude -p` (spawned e.g. by an LLM-call subprocess, or
# any other tool that shells out to `claude -p`) inherits its caller's cwd
# AND hook wiring, so its own SessionStart/SessionEnd fire this repo's hooks
# too — from underneath, and indistinguishably from, the real interactive
# session's own hook firings. Both hooks need to tell "this is the session
# that owns the lock" from "this is some other (possibly nested) session
# running the same hook" — hence `lock_pid_for` + `is_ancestor` below, used
# by both to refuse to steal/drop a lock that isn't theirs.

# find_session_pid <starting-pid>
#
# Walks the parent chain from <starting-pid> (typically the hook script's own
# $PPID) up to the durable `claude` / `claude -w <name>` / `claude -p ...`
# process this session lives and dies with. Prints that pid and returns 0, or
# returns 1 if the walk runs out (bounded at 25 hops so a broken /proc or a
# `ps` failure can't spin) without finding one.
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
# Either way this only matches the process itself, never a transient wrapper
# that merely mentions "claude"/the repo path somewhere in argv.
#
# Identifying the session by a `*claude*` SUBSTRING on the full command line
# is unsound and was rejected once already (gr172130): a short-lived
# intermediate wrapper (a shell-snapshot `sh -c "..."`, etc.) invoked with a
# worktree's own path in its argv also matches `*claude*`, and if the walk
# hits that wrapper before the real session process, it resolves to a pid
# that's already gone by the time anything checks `kill -0`. This exact
# discriminator is pinned by tests/test_worktree_lock_reap.py — do not
# "improve" the matching logic without re-reading that test first.
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

# is_ancestor <candidate-pid> <starting-pid>
#
# Returns 0 (true) if <candidate-pid> is <starting-pid> itself or appears
# anywhere further up its parent chain, 1 otherwise. Same bounded walk (25
# hops) and the same reasoning as find_session_pid: a broken /proc or a `ps`
# failure must fail closed (not-an-ancestor), never spin.
#
# gr256469: this is what tells "the lock is already held by the process I'm
# nested underneath" apart from "the lock is held by some unrelated pid" —
# the former must NOT be touched by a nested session's hook firing; the
# latter (stale/dead/someone-else's) is exactly what the existing
# unlock-then-lock / unlock-then-reap behaviour must still handle.
is_ancestor() {
    local target=$1 pid=$2 i ppid
    for i in $(seq 1 25); do
        [ -z "${pid:-}" ] && return 1
        case "$pid" in ''|*[!0-9]*) return 1 ;; esac
        [ "$pid" -le 1 ] && return 1
        [ "$pid" = "$target" ] && return 0

        ppid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d '[:space:]')
        [ -z "$ppid" ] && return 1
        pid=$ppid
    done
    return 1
}

# lock_pid_for <worktree-path>
#
# Reads `git worktree list --porcelain`'s `locked <reason>` line for
# <worktree-path> and prints the numeric pid out of a `pid <N>` reason — the
# only shape scripts/hooks/session-start-lock.sh ever writes (mirrors
# scripts/inflight's session_field(), the shared source of truth for this
# parse). Prints nothing and returns failure if the tree isn't locked, or is
# locked with a reason that isn't the `pid <N>` shape.
#
# Must be run from inside the repo (any worktree — `git worktree list` is
# repo-wide, not worktree-local). Resolves both sides to a realpath so a
# caller's non-canonical path (trailing slash, symlinked parent, etc.) still
# matches the porcelain output's own path.
lock_pid_for() {
    local target=$1 real_target wt_path wt_real matched=0 line reason pid=""
    real_target=$(cd "$target" 2>/dev/null && pwd -P) || real_target=$target
    while IFS= read -r line; do
        case "$line" in
            "worktree "*)
                wt_path=${line#worktree }
                wt_real=$(cd "$wt_path" 2>/dev/null && pwd -P) || wt_real=$wt_path
                if [ "$wt_real" = "$real_target" ]; then
                    matched=1
                else
                    matched=0
                fi
                ;;
            "locked"*)
                if [ "$matched" -eq 1 ]; then
                    reason=${line#locked}
                    reason=${reason# }
                    if [[ "$reason" =~ pid[[:space:]]+([0-9]+) ]]; then
                        pid="${BASH_REMATCH[1]}"
                    fi
                fi
                ;;
            "")
                matched=0
                ;;
        esac
    done < <(git worktree list --porcelain 2>/dev/null)
    [ -n "$pid" ] || return 1
    printf '%s' "$pid"
}

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
# fix. See `find_session_pid` in scripts/lib/session-lock.sh for the
# discriminator used instead.
#
# No-op for the PRIMARY checkout (only worktrees under .claude/worktrees/
# are ever locked) and idempotent (unlock-then-lock), so a restarted/resumed
# session just refreshes its own lock instead of erroring on "already locked"
# — UNLESS this invocation is itself a nested `claude -p` running underneath
# the session that already holds the lock (gr256469): see the
# lock_pid_for/is_ancestor guard below, right before the unlock/lock pair.
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

# find_session_pid (the discriminator walk described above) plus
# lock_pid_for/is_ancestor (gr256469's nested-session guard, right below) now
# live in scripts/lib/session-lock.sh, shared with session-end-reap.sh so the
# two hooks' notion of "whose session is this" can't drift apart. Guarded
# source, same pattern session-end-reap.sh already uses for
# scripts/lib/compose-project.sh: an older checkout without this file just
# leaves find_session_pid undefined, and the `|| exit 0` below turns that
# into a clean no-op instead of an error.
[ -f scripts/lib/session-lock.sh ] && source scripts/lib/session-lock.sh

# gr256469: a nested `claude -p` (e.g. spawned by an LLM-call subprocess)
# inherits THIS worktree's cwd and hook wiring, so its own SessionStart also
# fires this script — from underneath the real, already-locked interactive
# session. Run from that nested invocation, the walk below resolves to the
# nested `claude -p` itself (the nearest comm==claude ancestor), not the real
# session further up — so the unconditional unlock/lock pair that follows
# would silently steal the lock from the live outer session and hand it to
# the nested one-shot, which exits moments later and leaves a dead-lock a
# sibling's reaper then deletes the (still-live!) tree out from under.
#
# If the tree is ALREADY locked to a pid that is (a) alive right now and (b)
# an ancestor of this hook invocation, we ARE that nested case: leave the
# existing lock alone and exit, rather than re-locking to ourselves.
# Anything else — no lock, an unparseable reason, a dead pid, or a live pid
# that ISN'T our ancestor (e.g. a stale lock from an unrelated session, or
# this same session simply re-running its own SessionStart on resume) — falls
# through unchanged to today's idempotent unlock-then-lock below.
if command -v lock_pid_for >/dev/null 2>&1 && command -v is_ancestor >/dev/null 2>&1; then
    EXISTING_LOCK_PID=$(lock_pid_for "$HERE") || EXISTING_LOCK_PID=""
    if [ -n "$EXISTING_LOCK_PID" ] && kill -0 "$EXISTING_LOCK_PID" 2>/dev/null \
        && is_ancestor "$EXISTING_LOCK_PID" "${PPID:-}"; then
        exit 0
    fi
fi

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

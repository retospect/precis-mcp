#!/usr/bin/env bash
# SessionEnd hook: reap THIS session's own worktree immediately when it's
# already safe to remove, instead of waiting for the next session's
# scripts/reap-worktrees backstop to catch it.
#
# Reads the hook payload from stdin: {"reason": ..., "cwd": ..., ...}. Only
# acts on a genuine end-of-session signal — `reason` "clear" or "resume" means
# the session continues (context cleared/moved, worktree still active), so
# reaping there would destroy a live worktree. Proceeds only on
# logout / prompt_input_exit / other / bypass_permissions_disabled.
#
# Never reimplements liveness/merge detection: shells out to
# `scripts/inflight --json` and only removes THIS session's worktree — matched
# by `cwd` against an entry's `path` — if that entry's `bucket` is exactly
# `safe_remove`. Every other bucket (dirty, unmerged, live, primary) is left
# untouched, so unshipped work is never lost.
#
# Also releases the `git worktree lock` acquired at SessionStart by
# scripts/hooks/session-start-lock.sh — before the bucket check, so a
# live#<pid> session that's genuinely ending doesn't keep looking
# "locked+alive" to the next inflight/reap-worktrees run. NOT unconditional
# though (gr256469): see the ownership guard below, right before the unlock.
#
# Escape hatch: PRECIS_NO_AUTOREAP=1 → no-op.
# Fire-and-forget: SessionEnd's exit code/stdout aren't read by the harness —
# this is pure side effect and never reports back. Backstopped by
# scripts/reap-worktrees on the NEXT SessionStart if this is skipped/fails.
set -uo pipefail

[ -n "${PRECIS_NO_AUTOREAP:-}" ] && exit 0

PAYLOAD=$(cat)

REASON=$(printf '%s' "$PAYLOAD" | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("reason", ""))
except Exception:
    print("")' 2>/dev/null)
CWD=$(printf '%s' "$PAYLOAD" | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("cwd", ""))
except Exception:
    print("")' 2>/dev/null)

case "$REASON" in
    clear | resume) exit 0 ;;
esac

[ -z "$CWD" ] && exit 0
[ -d "$CWD" ] || exit 0

REPO_ROOT=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null) || exit 0
COMMON_DIR=$(git -C "$CWD" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || exit 0
PRIMARY=$(dirname "$COMMON_DIR")

# cd out of the worktree before touching it — `git worktree remove` refuses
# to run from inside the tree it's removing. The primary is a stable place
# to stand for the removal.
cd "$PRIMARY" 2>/dev/null || exit 0
command -v git >/dev/null 2>&1 || exit 0

# Per-worktree compose-project teardown (gr176375): this is the PRIMARY reap
# path (SessionStart's scripts/reap-worktrees is only the backstop), so the
# per-worktree test-DB project must be reclaimed here or it leaks in the common
# case. Guarded — an older primary checkout without the helper simply skips it.
[ -f scripts/lib/compose-project.sh ] && source scripts/lib/compose-project.sh

# find_session_pid/lock_pid_for/is_ancestor: shared with
# session-start-lock.sh, see scripts/lib/session-lock.sh. Same guarded
# source as compose-project.sh above — an older checkout without this file
# just leaves the functions undefined, and the `command -v` guard below
# degrades that into "always unlock", today's pre-fix behaviour.
[ -f scripts/lib/session-lock.sh ] && source scripts/lib/session-lock.sh

# gr256469: a nested `claude -p` (e.g. spawned by an LLM-call subprocess)
# inherits THIS worktree's cwd and hook wiring, so its own SessionEnd also
# fires this script — while the REAL interactive session that holds the lock
# is still very much alive and sitting in the tree. Unconditionally unlocking
# here (the pre-fix behaviour) drops the real session's lock out from under
# it: the tree then looks unlocked+merged+clean to the very next
# inflight/reap-worktrees run (this one's own bucket check below, or a
# sibling's), which deletes it while the real session is still using it —
# the exact mechanism behind gr256469's 1497-file loss.
#
# So: only unlock (and only then fall through to the reap/bucket check) if
# this ending session actually OWNS the lock — i.e. the lock is absent,
# unparseable, or names a pid that's already dead (all of which mean nothing
# alive depends on it), or names exactly THIS session's own pid (a genuine
# self-inflicted end). If it names a DIFFERENT, still-alive pid, we are not
# that session: leave the lock and the tree alone and get out now, before
# touching anything else.
# The guard FAILS SAFE, deliberately, in both directions: if we cannot prove
# the live lock is ours, we leave it alone. The two outcomes are not
# symmetric — leaving a lock behind is self-healing (its pid dies, the next
# run sees a dead lock and reclaims the tree normally), whereas dropping a
# live session's lock is the 1497-file data loss this guard exists to stop.
# So "can't tell" must resolve to "don't touch", never to "unlock anyway".
# Concretely that covers: the shared lib missing (an older primary checkout —
# note this script cd's to PRIMARY, so the source below resolves THERE, not
# in the worktree the hook shipped from), and find_session_pid failing to
# identify us at all. The cost of a false hold is an un-reaped worktree,
# which is visible, harmless and self-correcting.
LOCK_PID=""
OWN_PID=""
if command -v lock_pid_for >/dev/null 2>&1; then
    LOCK_PID=$(lock_pid_for "$REPO_ROOT") || LOCK_PID=""
fi
if command -v find_session_pid >/dev/null 2>&1; then
    OWN_PID=$(find_session_pid "${PPID:-}") || OWN_PID=""
fi
if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
    # Live lock: proceed ONLY on a positive identity match with this session.
    if ! { [ -n "$OWN_PID" ] && [ "$OWN_PID" = "$LOCK_PID" ]; }; then
        exit 0
    fi
elif ! command -v lock_pid_for >/dev/null 2>&1; then
    # No lib, so LOCK_PID above is "" for want of a reader, not because the
    # tree is genuinely unlocked — we can't distinguish those. Hold.
    exit 0
fi

git worktree unlock "$REPO_ROOT" >/dev/null 2>&1 || true

[ -x scripts/inflight ] || exit 0

JSON=$(scripts/inflight --json 2>/dev/null) || exit 0
[ -z "$JSON" ] && exit 0

TARGET=""
while IFS=$'\t' read -r path branch; do
    [ -z "$path" ] && continue
    TARGET="$path"
    TARGET_BRANCH="$branch"
done < <(printf '%s' "$JSON" | REPO_ROOT="$REPO_ROOT" python3 -c '
import json, os, sys
data = json.load(sys.stdin)
target = os.environ["REPO_ROOT"]
for wt in data.get("worktrees", []):
    if wt.get("path") == target and wt.get("bucket") == "safe_remove":
        print("\t".join([wt.get("path", ""), wt.get("branch", "")]))
        break
')

[ -z "$TARGET" ] && exit 0

if git worktree remove "$TARGET" 2>/dev/null; then
    # safe_remove already vetted mergedness (ancestor OR squash-absorbed);
    # -D deletes both, -d would refuse the squash case despite it being safe.
    git branch -D "$TARGET_BRANCH" >/dev/null 2>&1 || true
    # Reclaim this worktree's isolated test-DB project now the tree is gone —
    # `down -p <name>` finds it by label, no compose file needed (gr176375).
    # Best-effort: missing docker / an already-gone project must never matter
    # (this hook is fire-and-forget; its exit code is not read).
    if command -v docker >/dev/null 2>&1 && command -v compose_project_for >/dev/null 2>&1; then
        PROJ="$(compose_project_for "$TARGET" 2>/dev/null || true)"
        [ -n "$PROJ" ] && env UID="$(id -u)" GID="$(id -g)" \
            docker compose -p "$PROJ" down -v >/dev/null 2>&1 || true
    fi
fi
exit 0

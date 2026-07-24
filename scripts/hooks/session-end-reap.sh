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
fi
exit 0

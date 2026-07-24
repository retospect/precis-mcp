#!/usr/bin/env bash
# SessionStart hook: heal the PRIMARY checkout back onto main when it has
# drifted onto a feature branch that's fully merged, clean, and unheld by
# any live session.
#
# The primary drifts because sessions/agents sometimes work directly there
# (no `claude -w`) and check a branch out — guard-checkout-in-primary.py
# stops *new* drift at the source, this heals what's already there (or slips
# through before that guard was wired). scripts/ship never touches the
# primary's HEAD itself (it only fast-forwards `main` there via
# `merge --ff-only` / `update-ref`), so without this the primary just sits
# on the stale branch forever.
#
# Never reimplements liveness/merge detection: shells out to
# `scripts/inflight --json` for the "no live session holds it" check (its own
# session-lock/pid parsing), but the primary is a special row whose bucket
# depends on who's asking (`base`, `self`, or a plain verdict row) — so
# merged/clean are cross-checked directly against git
# (`status --porcelain` empty, `merge-base --is-ancestor`/`cherry`, the same
# tests inflight itself uses for its `merged`/`in-main` verdicts) rather than
# trusting one bucket label.
#
# Escape hatch: PRECIS_NO_HEAL_PRIMARY=1 → no-op.
# Wired in .claude/settings.json (SessionStart), after reap-worktrees.
set -uo pipefail

[ -n "${PRECIS_NO_HEAL_PRIMARY:-}" ] && exit 0

cd "$(dirname "$0")/../.." || exit 0
command -v git >/dev/null 2>&1 || exit 0
git rev-parse --git-dir >/dev/null 2>&1 || exit 0

BASE=main
git show-ref --verify --quiet refs/heads/main || BASE=master

PRIMARY=$(git worktree list --porcelain | awk '/^worktree /{print $2; exit}')
[ -z "$PRIMARY" ] && exit 0

BRANCH=$(git -C "$PRIMARY" symbolic-ref --quiet --short HEAD 2>/dev/null)
[ -z "$BRANCH" ] && exit 0          # detached HEAD — leave it alone
[ "$BRANCH" = "$BASE" ] && exit 0   # already healthy, nothing to do

# clean tree only — never switch branches under uncommitted work.
[ -n "$(git -C "$PRIMARY" status --porcelain 2>/dev/null)" ] && exit 0

# fully merged into base (ancestor OR squash-absorbed — the same two tests
# scripts/inflight uses for its 'merged'/'in-main' verdicts).
HEAD=$(git -C "$PRIMARY" rev-parse HEAD 2>/dev/null)
[ -z "$HEAD" ] && exit 0
if ! git -C "$PRIMARY" merge-base --is-ancestor "$HEAD" "$BASE" 2>/dev/null; then
    UNMERGED=$(git -C "$PRIMARY" cherry "$BASE" "$BRANCH" 2>/dev/null | grep -c '^+')
    [ "$UNMERGED" != 0 ] && exit 0
fi

# no live session holding the primary.
[ -x scripts/inflight ] || exit 0
JSON=$(scripts/inflight --json 2>/dev/null) || exit 0
[ -z "$JSON" ] && exit 0
LIVE=$(printf '%s' "$JSON" | PRIMARY="$PRIMARY" python3 -c '
import json, os, sys
data = json.load(sys.stdin)
target = os.environ["PRIMARY"]
for wt in data.get("worktrees", []):
    if wt.get("path") == target:
        print("live" if wt.get("session", "").startswith("live#") else "")
        break
')
[ "$LIVE" = "live" ] && exit 0

git -C "$PRIMARY" checkout "$BASE" >/dev/null 2>&1 || exit 0
# already vetted merged (ancestor OR squash-absorbed) above; -D deletes both,
# -d would refuse the squash case despite it being safe.
git -C "$PRIMARY" branch -D "$BRANCH" >/dev/null 2>&1 || true
echo "heal-primary-branch: primary was on '$BRANCH' (merged+clean) → checked out $BASE, deleted $BRANCH"
exit 0

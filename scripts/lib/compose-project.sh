# scripts/lib/compose-project.sh — shared derivation of a per-worktree Docker
# Compose project name (sourced, not executed).
#
# Why: the compose project name defaults to the basename of the compose file's
# directory, which is literally `dev` (`<worktree>/docker/dev/compose.yaml`) in
# EVERY worktree and the primary checkout. So all sibling worktrees' `scripts/
# test` / `scripts/ship` gates share ONE physical `precis-test-db` Postgres
# instance — a sibling's heavy `-n6` run OOM-crashes it into recovery mode and
# drags every concurrent gate down (gr176375; the `fsync=off` service config
# makes the crash's recovery slow/unreliable). Giving each worktree its own
# project name isolates the container/network/volume, matching the isolation the
# rest of the workflow already assumes (own branch, own .testmondata, own
# PRECIS_ROOT).
#
# Sourced by scripts/test + scripts/ship (which add `-p "$(compose_project_for
# "$WORKTREE")"` to their compose wrapper) and scripts/reap-worktrees (which
# tears the project down `-v` when it reaps the tree, so isolation doesn't leak
# containers as worktrees come and go). Keyed on the worktree's absolute
# physical path so the derivation is identical across all three callers.

compose_project_for() {
    local path="${1:?compose_project_for: worktree path required}"
    # Normalise to an absolute physical path so /a/b, /a/b/, and symlinked
    # variants resolve identically across callers (the reaper passes another
    # worktree's path; test/ship pass their own $PWD).
    path="$(cd "$path" 2>/dev/null && pwd -P || printf '%s' "$path")"
    # The worktree's directory basename is already unique + stable (git refuses
    # a duplicate worktree path, and the primary checkout's basename differs),
    # so it keys the project directly — no external hash tool, and the resulting
    # container names stay human-readable in `docker ps`. Sanitise to compose's
    # project-name charset ([a-z0-9][a-z0-9_-]*): map any other char to `-`
    # (pure-bash, works on macOS bash 3.2) and lowercase via `tr`. The map is
    # not injective (`foo.bar` and `foo bar` both → `foo-bar`), but worktree
    # names are minted as dash-words (`claude -w <adjective-verbing-noun>`) so a
    # collision needs two names differing only in a non-alnum char — not a shape
    # the generator produces. Callers derive the basename identically whether or
    # not `cd` succeeded (the reaper calls this AFTER the tree is gone): `pwd -P`
    # only canonicalises parent symlinks, never the final component.
    local base="${path##*/}"
    base="${base//[!a-zA-Z0-9_-]/-}"
    base="$(printf '%s' "$base" | tr '[:upper:]' '[:lower:]')"
    # Fail LOUD on an empty basename rather than emit a bare `precis-test-` that
    # would silently re-share ONE project across worktrees — the exact bug this
    # file exists to prevent. Under `set -e` (test/ship) the caller's assignment
    # aborts; the reaper (no `-e`) skips its best-effort teardown.
    if [ -z "$base" ]; then
        printf 'compose_project_for: empty project basename for %s\n' "${1}" >&2
        return 1
    fi
    printf 'precis-test-%s' "$base"
}

# shellcheck shell=bash
# bash 3.2 floor (stock macOS): no declare -A, mapfile, ${x,,}, &>, |&, coproc, globstar — tests/test_bash32_gotchas.py
# scripts/lib/autocatpath-wheel.sh — pure helpers for matching this repo's
# declared `autocatpath` version floor against the wheels a controller has
# actually built. Sourced by scripts/deploy; no side effects, so the version
# arithmetic can be tested on its own (tests/test_autocatpath_wheel.py).
#
# gr263082: autocatpath is release-gated off PyPI past 0.13.0, so the ONLY
# channel to a cluster host is `--find-links /opt/precis/wheels`, seeded by
# `deploy/roles/autocatpath/tasks/wheelhouse_seed.yml` from a controller-side
# wheel passed as `-e autocatpath_wheel=<path>`. That variable defaults to ""
# and nothing ever set it — so when pyproject's floor moved to >=0.18.0 while
# the newest built wheel was 0.17.0, the seed task was a silent no-op and the
# `precis-mcp[paper,catalyst]` install failed on exactly the autocatpath_plugin
# hosts, mid-run, leaving the fleet on mixed versions.
#
# The floor lives in THIS repo and the artifact lives in the catpath repo, so
# the two can always drift. These helpers exist so scripts/deploy can notice
# the drift in preflight — before it has touched any host — instead of finding
# out host by host.

# autocatpath_floor <pyproject-path>
#
# Prints the lowest `autocatpath>=<version>` floor declared anywhere in
# <pyproject-path>, or returns 1 if none is declared. Lowest, not first:
# pyproject states the floor more than once (the `catalyst` extra's pure
# engine and `catalyst-gpu`'s `autocatpath[mace]`), and a wheel only has to
# clear the lowest of them for SOME extra to resolve — anything stricter would
# abort a deploy that would in fact have succeeded.
autocatpath_floor() {
    local pyproject=$1 floor
    [ -f "$pyproject" ] || return 1
    floor=$(
        sed -n 's/.*autocatpath[^"]*>=\([0-9][0-9.]*\).*/\1/p' "$pyproject" |
            sort -V | head -1
    )
    [ -n "$floor" ] || return 1
    printf '%s' "$floor"
}

# version_ge <a> <b>
#
# True when version <a> is greater than or equal to <b>, compared the way
# `sort -V` orders release numbers. Deliberately not a PEP 440 implementation:
# these are plain X.Y.Z release wheels, and a half-right pre-release parser
# would be worse than an obviously-simple one.
version_ge() {
    local a=$1 b=$2
    [ "$a" = "$b" ] && return 0
    [ "$(printf '%s\n%s\n' "$a" "$b" | sort -V | head -1)" = "$b" ]
}

# newest_autocatpath_wheel <dist-dir>
#
# Prints the path of the highest-versioned `autocatpath-<v>-*.whl` in
# <dist-dir>, or returns 1 if the directory holds none. Highest rather than
# newest-by-mtime: a rebuild of an older version must not shadow a newer one
# that is already sitting there.
newest_autocatpath_wheel() {
    local dir=$1 wheel best="" best_v="" v
    [ -d "$dir" ] || return 1
    for wheel in "$dir"/autocatpath-*.whl; do
        [ -f "$wheel" ] || continue
        v=${wheel##*/autocatpath-}
        v=${v%%-*}
        case "$v" in ''|*[!0-9.]*) continue ;; esac
        if [ -z "$best" ] || version_ge "$v" "$best_v"; then
            best=$wheel
            best_v=$v
        fi
    done
    [ -n "$best" ] || return 1
    printf '%s' "$best"
}

# autocatpath_wheel_version <wheel-path>
#
# Prints the version segment of a wheel filename.
autocatpath_wheel_version() {
    local v=${1##*/autocatpath-}
    printf '%s' "${v%%-*}"
}

# autocatpath_project_version <pyproject-path>
#
# Prints the `version = "X.Y.Z"` a catpath CHECKOUT declares for itself — what
# a build from that tree would produce. Compared against the floor before
# building, so scripts/deploy never burns a build on a checkout that is too
# old to satisfy it anyway (the "pull catpath first" case).
autocatpath_project_version() {
    local pyproject=$1 v
    [ -f "$pyproject" ] || return 1
    v=$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$pyproject" | head -1)
    [ -n "$v" ] || return 1
    printf '%s' "$v"
}

# autocatpath_locked_sha <uv.lock-path>
#
# Prints the exact catpath commit this repo's lockfile pins, or returns 1 if
# autocatpath is not locked to a git source.
#
# gr263082 comment 1: catpath reuses one version number across many commits, so
# `autocatpath-0.18.0-py3-none-any.whl` does not identify a build and no version
# comparison can recover what it holds. The identifier we need already exists,
# though — `uv lock` records the resolved commit directly in uv.lock, and
# `uv lock -P autocatpath` is what moves it. Reading it here lets scripts/deploy
# check a checkout against what precis actually depends on, rather than against
# a proxy like "level with upstream" — not the same question, and it answers it
# wrong whenever catpath has moved ahead of the pin.
#
# Anchored to the `name = "autocatpath"` stanza rather than grepping the URL:
# uv.lock carries a `source = { git = ... }` line for every git dependency.
autocatpath_locked_sha() {
    local lock=$1 sha
    [ -f "$lock" ] || return 1
    sha=$(
        awk '/^name = "autocatpath"$/      { in_pkg = 1; next }
             in_pkg && /^\[\[/             { in_pkg = 0 }
             in_pkg && /^source = / && /#/ { print; exit }' "$lock" |
            sed -n 's/.*#\([0-9a-f]\{7,40\}\)".*/\1/p'
    )
    [ -n "$sha" ] || return 1
    printf '%s' "$sha"
}

# autocatpath_locked_version <uv.lock-path>
#
# Prints the version uv.lock records for the autocatpath package — the pinned
# commit's own declared version, captured when `uv lock` resolved it, so no
# catpath checkout is needed to read it (`git show <sha>:pyproject.toml`
# would say the same thing, at the cost of requiring the checkout). Returns
# 1 if autocatpath is not in the lock.
#
# gr499104: a `uv lock -P autocatpath` that moves only the pinned commit
# leaves the pyproject floor untouched, so "newest wheel >= floor" happily
# selected last release's wheel — r66 shipped autocatpath-0.31.0 while the
# pin declared 0.31.1, and the build-from-pin path never ran. Equality with
# THIS version is the check the floor cannot provide.
autocatpath_locked_version() {
    local lock=$1 v
    [ -f "$lock" ] || return 1
    v=$(
        awk '/^name = "autocatpath"$/ { in_pkg = 1; next }
             in_pkg && /^\[\[/        { in_pkg = 0 }
             in_pkg && /^version = /  { print; exit }' "$lock" |
            sed -n 's/^version = "\([^"]*\)".*/\1/p'
    )
    [ -n "$v" ] || return 1
    printf '%s' "$v"
}

# autocatpath_select_wheel <dist-dir> <floor> [pin-version]
#
# Prints the wheel scripts/deploy should seed, or returns 1 when <dist-dir>
# holds none that qualifies — the caller then takes the build path.
#
# With <pin-version> (autocatpath_locked_version's answer when uv.lock pins a
# commit), ONLY `autocatpath-<pin-version>-*.whl` qualifies: a floor-clearing
# wheel at any other version is a build of code precis does not depend on,
# older OR newer (gr499104). Without a pin the pre-gr499104 rule applies —
# the newest wheel at or above <floor> — since an index-resolved dep has no
# commit to match against.
autocatpath_select_wheel() {
    local dir=$1 floor=$2 pin_v=${3:-} wheel v
    if [ -n "$pin_v" ]; then
        version_ge "$pin_v" "$floor" || return 1
        for wheel in "$dir"/autocatpath-"$pin_v"-*.whl; do
            [ -f "$wheel" ] || continue
            printf '%s' "$wheel"
            return 0
        done
        return 1
    fi
    wheel=$(newest_autocatpath_wheel "$dir" 2>/dev/null) || return 1
    v=$(autocatpath_wheel_version "$wheel")
    version_ge "$v" "$floor" || return 1
    printf '%s' "$wheel"
}

# autocatpath_probe_dirs <repo-root> [home]
#
# Prints the ordered candidate list for a catpath checkout, one per line,
# first hit wins — so scripts/deploy can both walk it and, when nothing
# matches, name every tried path in the refusal (a bare "no checkout" leaves
# the operator guessing which layouts count as conventional).
# PRECIS_CATPATH_DIR beats all of these and never goes through here.
#
# The repo sibling is resolved through --git-common-dir, not <repo-root>/..:
# from a linked worktree (.claude/worktrees/<name>) the checkout's parent IS
# the worktrees dir, so repo/../catpath looks inside it — a candidate that
# can never exist, while the real sibling beside the main checkout is never
# tried. And the home-layout entry is work/project/code (singular): the
# "projects" spelling never existed on the controller, so the old probe
# silently fell through to the retired ~/catpath and every deploy died in
# preflight unless PRECIS_CATPATH_DIR was set.
autocatpath_probe_dirs() {
    local repo_root=$1 home=${2:-$HOME} common main_root
    common=$(git -C "$repo_root" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || true
    if [ -n "$common" ]; then
        main_root=$(dirname "$common")
    else
        main_root=$repo_root
    fi
    printf '%s\n' \
        "${main_root}/../catpath" \
        "${home}/work/project/code/catpath" \
        "${home}/catpath"
}

# autocatpath_find_checkout <repo-root> [home]
#
# Prints the resolved path of the first autocatpath_probe_dirs candidate
# holding a pyproject.toml, or returns 1 when none does — the caller then
# reports the probed list, which is exactly what probe_dirs prints.
autocatpath_find_checkout() {
    local cand
    while IFS= read -r cand; do
        if [ -f "${cand}/pyproject.toml" ]; then
            printf '%s' "$(cd "$cand" && pwd)"
            return 0
        fi
    done < <(autocatpath_probe_dirs "$@")
    return 1
}

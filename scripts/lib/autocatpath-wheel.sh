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

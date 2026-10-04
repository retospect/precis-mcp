# shellcheck shell=bash
# scripts/lib/docker-disk.sh — free-space preflight for the docker VM disk.
# Sourced by scripts/test (and, once wired, scripts/ship).
#
# Why: on 2026-10-03 the colima VM disk reached 98% (90 GB build cache, ~40
# idle containers). The uv cache then hit ENOSPC mid-run, so lint, gates and
# scripts/test died with no test output — which reads as a red gate. A run
# that cannot finish should refuse up front, in seconds, with the cause named.
#
# docker_free_gb          prints the integer GB free on the docker root fs
#                         inside the VM, or nothing when it cannot be read.
# docker_disk_preflight W returns 3 (message on stderr) when free space is under
#                         PRECIS_DOCKER_MIN_FREE_GB (default 10); warns and
#                         returns 0 on an unknown value, never refusing on it.
#                         PRECIS_DOCKER_DISK_CHECK=0 skips the check.
#
# Probe (measured on the colima host, 2026-10-03): `docker run --rm
# --pull=never --entrypoint df <local image> -Pk /` ~0.3 s. A container's
# overlay root is backed by the docker root fs, so `/` there reports exactly
# the disk that fills; no pull and no bind mount, so it works against any
# daemon (colima, Docker Desktop, remote context). Fallback when no local
# image answers: `colima ssh -- df -Pk /var/lib/docker` (~0.35 s). Every probe
# is alarm-bounded (PRECIS_DOCKER_DISK_TIMEOUT, default 10 s) so a wedged
# daemon can never hang the caller. macOS bash 3.2 compatible.

# _docker_disk_bounded <cmd...> — run under perl's alarm (macOS ships no
# `timeout`; perl is on both platforms). No perl: run unbounded.
_docker_disk_bounded() {
    if command -v perl >/dev/null 2>&1; then
        perl -e 'alarm shift; exec @ARGV' "${PRECIS_DOCKER_DISK_TIMEOUT:-10}" "$@"
    else
        "$@"
    fi
}

# _docker_df_avail_gb — reads `df -Pk` output on stdin, prints integer GB of
# the Available column, nothing when the shape is wrong.
_docker_df_avail_gb() {
    awk 'NF >= 6 && $4 ~ /^[0-9]+$/ { kb = $4 } END { if (kb != "") print int(kb / 1048576) }'
}

docker_free_gb() {
    local out gb img rc
    if command -v docker >/dev/null 2>&1; then
        for img in "${PRECIS_DOCKER_DISK_IMAGE:-}" precis-dev:latest alpine:latest \
            debian:bookworm-slim; do
            [ -n "$img" ] || continue
            rc=0
            out="$(_docker_disk_bounded docker run --rm --pull=never --entrypoint df \
                "$img" -Pk / 2>/dev/null)" || rc=$?
            # 142 = SIGALRM: the daemon is wedged, so another image won't help.
            [ "$rc" = 142 ] && break
            [ "$rc" = 0 ] || continue
            gb="$(printf '%s\n' "$out" | _docker_df_avail_gb)"
            if [ -n "$gb" ]; then
                printf '%s\n' "$gb"
                return 0
            fi
        done
    fi
    if command -v colima >/dev/null 2>&1; then
        out="$(_docker_disk_bounded colima ssh -- df -Pk /var/lib/docker 2>/dev/null)" || out=""
        gb="$(printf '%s\n' "$out" | _docker_df_avail_gb)"
        if [ -n "$gb" ]; then
            printf '%s\n' "$gb"
            return 0
        fi
    fi
    return 0
}

docker_disk_preflight() {
    local who="${1:-this run}" min free
    [ "${PRECIS_DOCKER_DISK_CHECK:-1}" = "0" ] && return 0
    min="${PRECIS_DOCKER_MIN_FREE_GB:-10}"
    case "$min" in '' | *[!0-9]*) min=10 ;; esac
    free="$(docker_free_gb)"
    if [ -z "$free" ]; then
        echo "WARNING: ${who}: could not read the docker VM's free disk space; proceeding without the check" >&2
        return 0
    fi
    if [ "$free" -lt "$min" ]; then
        echo "${who}: docker VM disk has ${free} GB free; run scripts/reap-test-dbs, or ask the orchestrator to prune; do not re-run" >&2
        return 3
    fi
    return 0
}

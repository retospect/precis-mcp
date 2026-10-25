# shellcheck shell=bash
# bash 3.2 floor (stock macOS): no declare -A, mapfile, ${x,,}, &>, |&, coproc, globstar — tests/test_bash32_gotchas.py
# scripts/lib/docker-disk.sh — free-space preflight for the docker VM disk.
# Sourced by scripts/test and scripts/ship.
#
# Why: on 2026-10-03 the colima VM disk reached 98% (90 GB build cache, ~40
# idle containers). The uv cache then hit ENOSPC mid-run, so lint, gates and
# scripts/test died with no test output — which reads as a red gate. A run
# that cannot finish should refuse up front, in seconds, with the cause named.
#
# docker_free_gb          prints the integer GB free on the docker root fs inside
#                         the VM, or "failed: <reason>" (return 1) when it cannot
#                         be read. A real 0 prints "0"; a failure never does.
# docker_disk_preflight W returns 3 (message on stderr) when free space is under
#                         PRECIS_DOCKER_MIN_FREE_GB (default 10) AND when the
#                         probe fails ("disk probe failed: <reason>"): a gate that
#                         cannot tell whether the disk is full must not start one,
#                         and must not call it full either (gr493188).
#                         PRECIS_DOCKER_DISK_CHECK=0 skips the check.
#
# Probe order, cheapest first. Leg 1 (gr508691): `docker exec <running cid>
# df -Pk /` — no container create, so a loaded or create-wedged daemon (the
# gripe's host: ~215 containers, `docker run` stalled past its bound while
# exec/info answered instantly) still answers, and a container's overlay `/`
# is backed by the same docker root fs the run-probe reads. Leg 2, for a host
# with nothing running: `docker run --rm --pull=never --entrypoint df
# <local image> -Pk /` (~0.3 s when the daemon is healthy). Fallback when no
# local image answers: `colima ssh -- df -Pk /var/lib/docker` (~0.35 s).
# Each attempt is bounded (PRECIS_DOCKER_DISK_TIMEOUT, default 30 s) and the
# whole probe by PRECIS_DOCKER_DISK_TOTAL_TIMEOUT (default 45 s), so a wedged
# daemon can never hang the caller. 30 s is the loaded-daemon bound, not the
# healthy one. macOS bash 3.2 compatible.

# _docker_disk_bounded <seconds> <cmd...> — run in its own process group under
# a perl deadline (macOS ships no `timeout`). On expiry the whole group is
# killed, so a grandchild (colima's ssh) cannot hold the caller's $(...) pipe
# open past the bound; exits 142. No perl: run unbounded.
_docker_disk_bounded() {
    local secs="$1"
    shift
    if command -v perl >/dev/null 2>&1; then
        perl -e '
            my $t = shift; my $p = fork();
            if (!$p) { setpgrp(0, 0); exec @ARGV or exit 127 }
            $SIG{ALRM} = sub { kill "KILL", -$p; waitpid($p, 0); exit 142 };
            alarm $t; waitpid($p, 0);
            exit($? & 127 ? 128 + ($? & 127) : $? >> 8);
        ' "$secs" "$@"
    else
        "$@"
    fi
}

# _docker_disk_now — elapsed seconds for the probe budget. Normally bash
# SECONDS; tests point PRECIS_DOCKER_DISK_CLOCK at a file holding integer
# seconds (the fake docker advances it per attempt) so the overall-bound math
# is exact under load. A missing/non-numeric file falls back to SECONDS.
_docker_disk_now() {
    local f="${PRECIS_DOCKER_DISK_CLOCK:-}" v
    if [ -n "$f" ]; then
        v="$(cat "$f" 2>/dev/null)"
        case "$v" in
            '' | *[!0-9]*) ;;
            *) echo "$v"; return ;;
        esac
    fi
    echo "$SECONDS"
}

# _docker_df_avail_gb — reads `df -Pk` output on stdin, prints integer GB of
# the Available column, nothing when the shape is wrong.
_docker_df_avail_gb() {
    awk 'NF >= 6 && $4 ~ /^[0-9]+$/ { kb = $4 } END { if (kb != "") print int(kb / 1048576) }'
}

docker_free_gb() {
    local out gb img cid cids rc per per_try total deadline left why=""
    per="${PRECIS_DOCKER_DISK_TIMEOUT:-30}"
    total="${PRECIS_DOCKER_DISK_TOTAL_TIMEOUT:-45}"
    # 0 would mean "no alarm" to perl, i.e. no bound: treat it as unset.
    case "$per" in '' | 0 | *[!0-9]*) per=30 ;; esac
    case "$total" in '' | 0 | *[!0-9]*) total=45 ;; esac
    deadline=$(($(_docker_disk_now) + total))
    if command -v docker >/dev/null 2>&1; then
        # Cheap leg (see header): df inside an already-running container. A
        # hanging exec means a deeper wedge — `docker run` won't answer
        # either, so a 142 here skips the create leg entirely.
        rc=0
        cids="$(_docker_disk_bounded "$per" docker ps -q </dev/null 2>/dev/null \
            | head -3)" || rc=$?
        for cid in $cids; do
            left=$((deadline - $(_docker_disk_now)))
            if [ "$left" -le 0 ]; then
                why="${why:+$why; }overall ${total}s probe bound hit"
                break
            fi
            per_try="$per"
            [ "$left" -lt "$per" ] && per_try="$left"
            rc=0
            out="$(_docker_disk_bounded "$per_try" docker exec "$cid" \
                df -Pk / </dev/null 2>/dev/null)" || rc=$?
            if [ "$rc" = 142 ]; then
                why="${why:+$why; }docker exec timed out after ${per_try}s"
                break
            fi
            [ "$rc" != 0 ] && continue
            gb="$(printf '%s\n' "$out" | _docker_df_avail_gb)"
            if [ -n "$gb" ]; then
                printf '%s\n' "$gb"
                return 0
            fi
            # No df row (a distroless/scratch container) — try the next cid.
        done
        # An exec timeout (rc 142) means the daemon can't even serve exec —
        # container create won't answer either, so skip the run leg.
        if [ "${rc:-0}" != "142" ]; then
        for img in "${PRECIS_DOCKER_DISK_IMAGE:-}" precis-dev:latest alpine:latest \
            debian:bookworm-slim; do
            [ -n "$img" ] || continue
            case "$img" in
                -*) # docker run reads it as a flag and prints its usage text
                    why="${why:+$why; }image '$img' looks like a flag"
                    continue
                    ;;
            esac
            left=$((deadline - $(_docker_disk_now)))
            if [ "$left" -le 0 ]; then
                why="${why:+$why; }overall ${total}s probe bound hit"
                break
            fi
            per_try="$per"
            [ "$left" -lt "$per" ] && per_try="$left"
            rc=0
            out="$(_docker_disk_bounded "$per_try" docker run --rm --pull=never \
                --entrypoint df "$img" -Pk / </dev/null 2>/dev/null)" || rc=$?
            # 142 = our deadline: the daemon is wedged, another image won't help.
            if [ "$rc" = 142 ]; then
                why="${why:+$why; }docker run timed out after ${per_try}s"
                break
            fi
            if [ "$rc" != 0 ]; then
                why="${why:+$why; }docker run $img exited $rc"
                continue
            fi
            gb="$(printf '%s\n' "$out" | _docker_df_avail_gb)"
            if [ -n "$gb" ]; then
                printf '%s\n' "$gb"
                return 0
            fi
            why="${why:+$why; }docker run $img gave no df row"
        done
        fi
    else
        why="docker not on PATH"
    fi
    left=$((deadline - $(_docker_disk_now)))
    if command -v colima >/dev/null 2>&1 && [ "$left" -gt 0 ]; then
        per_try="$per"
        [ "$left" -lt "$per" ] && per_try="$left"
        out="$(_docker_disk_bounded "$per_try" colima ssh -- df -Pk /var/lib/docker </dev/null 2>/dev/null)" || out=""
        gb="$(printf '%s\n' "$out" | _docker_df_avail_gb)"
        if [ -n "$gb" ]; then
            printf '%s\n' "$gb"
            return 0
        fi
        why="${why:+$why; }colima ssh df gave no answer"
    fi
    printf 'failed: %s\n' "${why:-no probe available}"
    return 1
}

docker_disk_preflight() {
    local who="${1:-this run}" min free
    [ "${PRECIS_DOCKER_DISK_CHECK:-1}" = "0" ] && return 0
    min="${PRECIS_DOCKER_MIN_FREE_GB:-10}"
    case "$min" in '' | *[!0-9]*) min=10 ;; esac
    free="$(docker_free_gb)" || true
    case "$free" in
        '' | failed:*)
            echo "${who}: disk probe failed: ${free#failed: }; this is not a disk-full verdict. Re-run, or PRECIS_DOCKER_DISK_CHECK=0 to skip the probe" >&2
            return 3
            ;;
    esac
    if [ "$free" -lt "$min" ]; then
        echo "${who}: docker VM disk has ${free} GB free; run scripts/reap-test-dbs, or ask the orchestrator to prune; do not re-run" >&2
        return 3
    fi
    return 0
}

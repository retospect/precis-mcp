#!/usr/bin/env bash
# bash 3.2 floor (stock macOS): no declare -A, mapfile, ${x,,}, &>, |&, coproc, globstar — tests/test_bash32_gotchas.py
# scripts/lib/testmon-retry.sh — run a `pytest --testmon` step; when (and only
# when) it dies with an INTERNALERROR naming testmon, drop the stale map so the
# NEXT --checked run uses the plain (non-testmon) selection.
#
# Why: a .testmondata left stale by rebases makes testmon crash inside its own
# bookkeeping (INTERNALERROR> ... testmon ...), and every later run re-crashes
# on the same map. The step still FAILS (exit status passed through): testmon
# frames can sit in the traceback of an unrelated INTERNALERROR, and the tests
# testmon would have selected did not run, so a green here would be unearned.
# The fix is "drop the map and say re-run", never "carry on green".
#
# Usage: testmon-retry.sh <datafile> <command...>
#   <datafile>  the TESTMON_DATAFILE; on the fallback it and its -wal/-shm
#               sidecars are removed (no empty map: the next --checked run must
#               see "no map" and use the plain selection, not rebuild one) and
#               <datafile>.dropped is written so ship removes the host copy too.
set -u
datafile="$1"; shift
log="$(mktemp "${TMPDIR:-/tmp}/testmon-retry.XXXXXX")"
rc_file="$log.rc"
trap 'rm -f "$log" "$rc_file"' EXIT

( "$@"; echo $? >"$rc_file" ) 2>&1 | tee "$log"
rc="$(cat "$rc_file" 2>/dev/null || echo 1)"

[[ "$rc" == 0 ]] && exit 0
# INTERNALERROR is pytest's crash-in-the-harness banner; require testmon in the
# traceback so an INTERNALERROR from any other plugin still fails the gate.
if grep -q 'INTERNALERROR>' "$log" && grep -q 'INTERNALERROR>.*testmon' "$log"; then
    echo "testmon INTERNALERROR (rc=${rc}) — testmon map dropped as stale; re-run scripts/ship --checked (it will use the plain selection)" >&2
    rm -f "${datafile}" "${datafile}-wal" "${datafile}-shm"
    # Marker for scripts/ship's testmon_from_container: with no map left in the
    # container it would otherwise keep the (stale) host copy. The name matches
    # the gitignored `.testmondata*` glob.
    : >"${datafile}.dropped"
fi
exit "$rc"

# shellcheck shell=bash
# bash 3.2 floor (stock macOS): no declare -A, mapfile, ${x,,}, &>, |&, coproc, globstar — tests/test_bash32_gotchas.py
# scripts/lib/quiet-log.sh — quiet-mode epilogue for scripts/ship and
# scripts/rebase (gr497896). Their transcripts are too heavy for desk and
# coordinator panes, so by default the full run goes to a /tmp log and the
# terminal sees only:
#
#   · the ✖ lines (with their indented continuation lines), on stderr —
#     the channel die() and the gate-failure reports already use
#   · Python Traceback blocks (from "Traceback" through the exception
#     line) and lines matching error|Error|FAILED — a crash that never
#     reached a die() stays readable
#   · on a non-zero exit, the last 5 log lines (the end state, whatever
#     shape the failure took)
#   · on a conflict, the conflicted file list — names, never diffs
#   · the selector's two advisory notes: the coverage "static-only (wide)
#     selection" warning and the slow-lane note naming selected targets a
#     '-m not slow' lane collects 0 tests from (gr543576)
#   · "full log: <path>"
#   · the status token, LAST: the log's own "<NAME>: <token>" line when it
#     has one (scripts/rebase emits REBASE_STATUS per exit), else derived:
#     rc 0 → the ok token, unmerged paths or an in-progress rebase/merge →
#     conflict, anything else → error.
#
# Callers run the real script as `PROG_QUIET=1 bash "$0" ... >log 2>&1`,
# capture the rc, then call this; --verbose skips the wrap entirely.
# Everything here is guarded — the epilogue must never die under set -e
# and so mask the run's real exit code.
#
#   quiet_epilogue <log> <rc> <STATUS_NAME> [ok-token]

quiet_epilogue() {
    local log="$1" rc="$2" name="$3" ok="${4:-done}" status="" files=""
    # Failure blocks, on stderr where the originals went:
    #   ✖ line + indented continuations (the NETWORK failure's second
    #   line, the release conflict's path list, the remote gate's
    #   failing-job lines are all written indented); a Traceback through
    #   its first unindented colon line (the exception summary); and any
    #   error|Error|FAILED line a bare crash leaves behind.
    awk '/✖/{c=1;print;next}
         c&&/^[ \t]/{print;next}
         {c=0}
         /Traceback/{t=1;print;next}
         t&&/^[^ \t]/&&/:/{print;t=0;next}
         t{print;next}
         /error|Error|FAILED/{print}
         /coverage: no measured DB|slow-lane:/{print}' "$log" >&2 || true
    if [ "$rc" -ne 0 ]; then
        files="$(git diff --name-only --diff-filter=U 2>/dev/null || true)"
        if [ -n "$files" ]; then
            printf 'conflicted files:\n%s\n' "$files" >&2
        fi
        # A crash with no ✖ is still readable: the log's end state.
        tail -5 "$log" >&2 || true
    fi
    status="$(grep -o "${name}: .*" "$log" 2>/dev/null | tail -1 || true)"
    if [ -z "$status" ]; then
        if [ "$rc" -eq 0 ]; then
            status="$name: $ok"
        elif [ -n "$files" ] \
            || [ -d "$(git rev-parse --git-path rebase-merge 2>/dev/null || true)" ] \
            || [ -d "$(git rev-parse --git-path rebase-apply 2>/dev/null || true)" ] \
            || git rev-parse -q --verify MERGE_HEAD >/dev/null 2>&1; then
            status="$name: conflict"
        else
            status="$name: error"
        fi
    fi
    # ship's success report names the landed sha — the one fact a desk needs
    # without opening the log.
    if [ "$rc" -eq 0 ]; then
        awk '/landed on main/{getline;print;exit}' "$log" 2>/dev/null || true
    fi
    # ship's post-land status writes (scripts/lib/land_notify.py) print one
    # land-notify line to the transcript only; a builder reading the short
    # capture saw no todo close and re-posted it by hand (gr542762). Forward
    # every land-notify line — result, skip and failure shapes — next to the
    # status token. A rebase log has none; the grep is a no-op there.
    grep '^land-notify' "$log" 2>/dev/null || true
    printf 'full log: %s\n%s\n' "$log" "$status"
}

# scripts/cadence-lib.sh — sourced (never executed) by the advisory review
# cadence scripts: token-review, db-thrash-review, gripe-gc-review,
# secrets-check-review, surface-review, fda-grant-review, skill-search-review.
#
# The clock is ~/.cache/precis/<script>.log, NOT the tracked runbook: an append
# into docs/runbooks dirtied every checkout the pass ran in, and the dated lines
# are temporary state that does not belong in the knowledge mesh (Reto
# 2026-10-30). `scripts/<script> --stamp "<note>"` appends one
# `**YYYY-MM-DD** — <note>` line there; the DUE/ok decision reads the newest.
#
# Usage, after `cd` to the repo root:
#   source scripts/cadence-lib.sh
#   cadence_init <script-name> <window-days> <runbook> "$@"   # --stamp exits here
#   cadence_check <verb: pass|check> "<what the pass does>"    # prints; rc 1 = DUE

# portable YYYY-MM-DD -> epoch seconds (BSD date on macOS, GNU date on CI/Linux)
epoch_of() { date -u -j -f "%Y-%m-%d" "$1" +%s 2>/dev/null || date -u -d "$1" +%s 2>/dev/null; }

cadence_init() {
    CADENCE_NAME="$1"; CADENCE_WINDOW="$2"; CADENCE_RUNBOOK="$3"; shift 3
    CADENCE_LOG="$HOME/.cache/precis/$CADENCE_NAME.log"
    if [[ "${1:-}" == "--stamp" ]]; then
        shift
        local note="${*:-}"
        note="${note//$'\n'/ }"
        if [[ -z "${note//[[:space:]]/}" ]]; then
            echo "$CADENCE_NAME: --stamp needs a one-line note: scripts/$CADENCE_NAME --stamp \"<note>\"" >&2
            exit 2
        fi
        mkdir -p "$(dirname "$CADENCE_LOG")"
        local line
        line="$(printf '**%s** — %s' "$(date -u +%F)" "$note")"
        printf '%s\n' "$line" >> "$CADENCE_LOG"
        echo "$line"
        exit 0
    fi
}

# Newest dated line of the log. Appended oldest-first, so take the
# chronologically LATEST (`sort | tail -1`; ISO dates sort lexically) — a bare
# `head -1` reads the OLDEST. Permissive leading markdown punctuation: the lines
# are `**bold**` today, and demanding bold is what froze other clocks.
cadence_last() {
    grep -oE '^[-#*[:space:]]*[0-9]{4}-[0-9]{2}-[0-9]{2}' "$CADENCE_LOG" 2>/dev/null \
        | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' | sort | tail -1 || true
}

# cadence_check <noun> <action>: prints the DUE/ok line; returns 1 when DUE so
# the caller can add its best-effort extras, 0 otherwise. Always prints, never
# fails the script (callers `exit 0`).
cadence_check() {
    local noun="$1" action="$2" last last_e days
    local due="$CADENCE_NAME: DUE"
    local tail_="$action; then scripts/$CADENCE_NAME --stamp \"<note>\". See $CADENCE_RUNBOOK."
    last=$(cadence_last)
    if [[ -z "$last" ]]; then
        echo "$due (no dated line in $CADENCE_LOG) — $tail_"; return 1
    fi
    last_e=$(epoch_of "$last" || true)
    if [[ -z "$last_e" ]]; then
        echo "$due (last entry '$last' unparseable — check $CADENCE_LOG) — $tail_"; return 1
    fi
    days=$(( ($(date +%s) - last_e) / 86400 ))
    if (( days > CADENCE_WINDOW )); then
        echo "$due (last $noun $last, ${days}d ago > ${CADENCE_WINDOW}d) — $tail_"; return 1
    fi
    echo "$CADENCE_NAME: ok (last $noun $last, ${days}d ago; due in $(( CADENCE_WINDOW - days ))d)"
    return 0
}

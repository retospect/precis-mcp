#!/usr/bin/env bash
# scripts/lib/backup-window.sh — the nightly-backup no-go window for
# scripts/deploy: refuse a MIGRATION deploy while the prod pg_dump may be
# running, warn-and-proceed for a code-only deploy.
#
# Why: the prod DB node reboots at 03:00 UTC and the nightly `pg_dump --jobs=4`
# runs 03:30 → ~04:18 UTC. The dump holds ACCESS SHARE on every table for its
# whole run, so a migration's `ALTER TABLE … ADD COLUMN` (ACCESS EXCLUSIVE)
# waits for the whole dump. 2026-09-08: a /go at 03:22 UTC hung 17+ min on
# the migration task with a silent log. Aborting mid-deploy is worse than
# waiting (new code installed, migrations unapplied), so the only good move is
# to not start. Until this file the rule lived in operator memory
# (docs/runbooks/migration-deploy-window.md).
#
# One source of truth for the bounds: deploy/roles/backups/defaults/main.yml,
# the same file that holds the pg_dump cron slot, so a schedule change moves
# both. Read with awk (plain `key: "HH:MM"` lines), not a YAML parser —
# nothing else in scripts/ depends on python being on PATH.
#
# "Pending migration" is computed OFFLINE, never by querying prod: a migration
# is pending when src/precis/migrations/*.sql has files ADDED between the sha
# origin/prod records (what the cluster runs — scripts/lib/env-pointers.sh)
# and the sha being deployed. The deploy applies exactly those. If either end
# cannot be resolved the answer is "unknown", and inside the window unknown
# refuses (fail closed) — the override flag is the way through.
#
# Clock: DEPLOY_NOW_UTC=HH:MM overrides `date -u` (tests, and an operator
# rehearsing the message). Comparison is minute-granular, half-open
# [start, end): 04:20 itself is outside.
#
# Usage:  . "$(dirname "$0")/lib/backup-window.sh"
#         backup_window_guard "$REPO_ROOT" "$REF" "$IGNORE_BACKUP_WINDOW" || exit 1
#
# Return codes of backup_window_guard / backup_window_decide:
#   0  proceed (outside the window, or inside with nothing pending — a warning
#      was printed — or inside on an explicit override)
#   1  refuse (inside the window with a pending or unknown migration set)
#   2  the bounds file is missing/unparseable — a tree problem, not a clock one

# Where the bounds live. One place; the test asserts the cron slot is inside.
backup_window_defaults_file() {
    printf '%s/deploy/roles/backups/defaults/main.yml\n' "${1:-$PWD}"
}

# Echo "<start> <end>" (HH:MM HH:MM) read from the defaults file. 2 if the
# file or either key is missing or not HH:MM.
backup_window_bounds() {
    local root="${1:-$PWD}" file start end
    file="$(backup_window_defaults_file "$root")"
    [[ -f "$file" ]] || {
        printf 'backup-window: no %s — cannot read the window bounds\n' "$file" >&2
        return 2
    }
    start="$(_backup_window_yaml_value backup_window_start_utc "$file")"
    end="$(_backup_window_yaml_value backup_window_end_utc "$file")"
    if ! _backup_window_is_hhmm "$start" || ! _backup_window_is_hhmm "$end"; then
        printf 'backup-window: %s must hold backup_window_start_utc/backup_window_end_utc as "HH:MM" (got start=%s end=%s)\n' \
            "$file" "${start:-unset}" "${end:-unset}" >&2
        return 2
    fi
    printf '%s %s\n' "$start" "$end"
}

# The pg_dump cron slot from the same file, as HH:MM — so a test can assert
# the slot sits inside the window without parsing YAML itself.
backup_window_cron_slot() {
    local root="${1:-$PWD}" file hour minute
    file="$(backup_window_defaults_file "$root")"
    [[ -f "$file" ]] || return 2
    hour="$(_backup_window_yaml_value pg_backup_cron_hour "$file")"
    minute="$(_backup_window_yaml_value pg_backup_cron_minute "$file")"
    [[ "$hour" =~ ^[0-9]{1,2}$ && "$minute" =~ ^[0-9]{1,2}$ ]] || return 2
    printf '%02d:%02d\n' "$((10#$hour))" "$((10#$minute))"
}

# HH:MM now, UTC. DEPLOY_NOW_UTC wins when set (must be HH:MM).
backup_window_now() {
    if [[ -n "${DEPLOY_NOW_UTC:-}" ]]; then
        _backup_window_is_hhmm "$DEPLOY_NOW_UTC" || {
            printf 'backup-window: DEPLOY_NOW_UTC=%s is not HH:MM\n' "$DEPLOY_NOW_UTC" >&2
            return 2
        }
        printf '%s\n' "$DEPLOY_NOW_UTC"
        return 0
    fi
    date -u +%H:%M
}

# 0 when <now> is inside [<start>, <end>). All three HH:MM. A window that
# wraps midnight (start > end) is handled too, though ours does not.
backup_window_contains() {
    local start end now
    start="$(_backup_window_minutes "$1")"
    end="$(_backup_window_minutes "$2")"
    now="$(_backup_window_minutes "$3")"
    if (( start <= end )); then
        (( now >= start && now < end ))
    else
        (( now >= start || now < end ))
    fi
}

# Echo the migration files ADDED between origin/prod and <target>, one per
# line (empty = nothing pending). 2 when either end does not resolve — the
# caller treats that as "unknown". <prod_ref> defaults to origin/prod; the
# fetch that keeps it fresh belongs to the caller (scripts/deploy), so this
# stays offline and a throwaway repo without a remote can drive it.
deploy_pending_migrations() {
    local root="$1" target="$2" prod_ref="${3:-origin/prod}" prod_sha target_sha
    prod_sha="$(git -C "$root" rev-parse -q --verify "${prod_ref}^{commit}" 2>/dev/null || true)"
    target_sha="$(git -C "$root" rev-parse -q --verify "${target}^{commit}" 2>/dev/null || true)"
    if [[ -z "$prod_sha" ]]; then
        printf 'backup-window: %s does not resolve here — cannot tell which migrations this deploy carries (git fetch origin prod)\n' "$prod_ref" >&2
        return 2
    fi
    if [[ -z "$target_sha" ]]; then
        printf 'backup-window: target %s does not resolve to a commit here — cannot tell which migrations this deploy carries\n' "$target" >&2
        return 2
    fi
    git -C "$root" diff --name-only --diff-filter=A "${prod_sha}..${target_sha}" -- src/precis/migrations/ 2>/dev/null \
        | grep -E '\.sql$' || true
}

# The decision, with every input explicit so a test can fake the clock and
# the pending set:
#   backup_window_decide <start> <end> <now> <pending> <ignore>
# <pending> is the newline-separated file list, "" for none, or the literal
# word `unknown`. <ignore> is 1 for --ignore-backup-window. Prints the
# refusal/warning/override note; return codes as in the header.
backup_window_decide() {
    local start="$1" end="$2" now="$3" pending="$4" ignore="${5:-0}" n list
    backup_window_contains "$start" "$end" "$now" || return 0

    if [[ "$pending" == unknown ]]; then
        n=unknown
        list=""
    elif [[ -z "$pending" ]]; then
        n=0
        list=""
    else
        n="$(printf '%s\n' "$pending" | grep -c .)"
        list="$(printf '%s\n' "$pending" | sed 's/^/    /')"
    fi

    if [[ "$n" == 0 ]]; then
        printf 'WARNING: inside the %s–%s UTC backup window (now %s UTC). No pending migration, so proceeding — but the DB node reboots at %s UTC and the nightly pg_dump runs through the window; expect dropped connections. Window ends %s UTC.\n' \
            "$start" "$end" "$now" "$start" "$end"
        return 0
    fi

    if [[ "$ignore" == 1 ]]; then
        printf 'NOTE: --ignore-backup-window — inside the %s–%s UTC backup window (now %s UTC) with %s pending migration(s); proceeding on operator override. If pg_dump is running, the migration task will block until it finishes (~%s UTC).\n' \
            "$start" "$end" "$now" "$n" "$end"
        [[ -z "$list" ]] || printf '%s\n' "$list"
        return 0
    fi

    if [[ "$n" == unknown ]]; then
        printf 'REFUSING TO DEPLOY: inside the %s–%s UTC backup window (now %s UTC) and this deploy'"'"'s pending-migration set is UNKNOWN (see the backup-window line above) — treated as a migration deploy (fail closed).\n' \
            "$start" "$end" "$now"
    else
        printf 'REFUSING TO DEPLOY: inside the %s–%s UTC backup window (now %s UTC) and this deploy carries %s pending migration(s):\n%s\n' \
            "$start" "$end" "$now" "$n" "$list"
    fi
    printf 'The nightly pg_dump holds ACCESS SHARE on every table, so the migration'"'"'s DDL would block behind it and the deploy would hang with a silent log (2026-09-08). Aborting mid-deploy is worse than waiting. Wait until the window ends at %s UTC, or — after checking the dump is not running (`ps -eo pid,etime,args | grep pg_dump` on the DB node) — re-run with --ignore-backup-window. docs/runbooks/migration-deploy-window.md.\n' "$end"
    return 1
}

# The whole guard for scripts/deploy: bounds from the tree, clock from
# DEPLOY_NOW_UTC/date, pending set from git.
#   backup_window_guard <repo_root> <target> [ignore] [prod_ref]
backup_window_guard() {
    local root="$1" target="$2" ignore="${3:-0}" prod_ref="${4:-origin/prod}" bounds start end now pending
    bounds="$(backup_window_bounds "$root")" || return 2
    read -r start end <<< "$bounds"
    now="$(backup_window_now)" || return 2
    # Outside the window nothing else is computed (and nothing printed):
    # unchanged behaviour is the contract there.
    backup_window_contains "$start" "$end" "$now" || return 0
    if ! pending="$(deploy_pending_migrations "$root" "$target" "$prod_ref")"; then
        pending=unknown
    fi
    backup_window_decide "$start" "$end" "$now" "$pending" "$ignore"
}

_backup_window_yaml_value() {
    # First `<key>: <value>` line; strips quotes and a trailing comment.
    awk -v key="$1" '
        $1 == key ":" {
            v = $2
            sub(/#.*/, "", v)
            gsub(/["\047[:space:]]/, "", v)
            print v
            exit
        }' "$2"
}

_backup_window_is_hhmm() {
    [[ "$1" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]
}

_backup_window_minutes() {
    local hhmm="$1"
    printf '%d\n' "$(( 10#${hhmm%%:*} * 60 + 10#${hhmm##*:} ))"
}

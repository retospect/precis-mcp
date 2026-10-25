#!/usr/bin/env bash
# scripts/lib/backup-window.sh — the nightly-backup no-go windows for
# scripts/deploy: refuse a MIGRATION deploy while a prod pg_dump may be
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
# Windows are a LIST (gr513885): the early-morning reboot+dump leg is only the
# first — the incremental-leg directory dump fires a second pg_dump nightly at
# 21:45 UTC (~60–90 min) with the same lock profile. backup_window_windows
# reads every `<X>_window_start_utc`/`<X>_window_end_utc` pair from the
# backups defaults file, each paired with the `pg_<X>_cron_*` slot it mirrors,
# and the guard applies the decision to whichever window holds the clock.
#
# The live check (gr511981): the clock is only a proxy. r91 resumed at
# 04:22Z — legitimately outside the window — while the dump still ran, and
# died on a lock timeout mid-migration. So for a migration deploy the guard
# also queries pg_stat_activity on prod (backup_window_live_backups): a
# running pg_dump/pg_basebackup refuses at ANY hour. Inside the window a
# 'clear' probe overrides the clock only once the scheduled cron slot has
# passed — before it, "nothing running now" says nothing about the dump that
# fires at the slot. When the live query cannot run (no prod-psql, prod
# unreachable) the fixed window is the fallback and decides exactly as
# before. The probe runs only when a migration may be in play; code-only
# deploys keep the probe-free path.
#
# One source of truth for the bounds: deploy/roles/backups/defaults/main.yml,
# the same file that holds each pg_dump cron slot, so a schedule change moves
# both. Read with awk (plain `key: "HH:MM"` lines), not a YAML parser —
# nothing else in scripts/ depends on python being on PATH. The convention a
# new backup leg follows: cron keys `pg_<X>_cron_hour`/`pg_<X>_cron_minute`,
# window keys `<X>_window_start_utc`/`<X>_window_end_utc`.
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
#      was printed — or inside on an explicit override, or cleared by the
#      live probe after the cron slot)
#   1  refuse (a backup is live on prod, or inside the window with a pending
#      or unknown migration set)
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

# Every deploy no-go window, one "<start> <end> <slot>" line per
# `<X>_window_*_utc` pair in the defaults file, in file order (the primary
# 03:00–04:20 leg first). <slot> is the matching `pg_<X>_cron_*` time as
# HH:MM, or empty when that leg has none — backup_window_decide treats it as
# "still ahead". 2 when the file is missing or a pair is not HH:MM.
backup_window_windows() {
    local root="${1:-$PWD}" file prefix start end hour minute slot
    file="$(backup_window_defaults_file "$root")"
    [[ -f "$file" ]] || {
        printf 'backup-window: no %s — cannot read the window bounds\n' "$file" >&2
        return 2
    }
    while read -r prefix; do
        start="$(_backup_window_yaml_value "${prefix}_window_start_utc" "$file")"
        end="$(_backup_window_yaml_value "${prefix}_window_end_utc" "$file")"
        if ! _backup_window_is_hhmm "$start" || ! _backup_window_is_hhmm "$end"; then
            printf 'backup-window: %s must hold %s_window_start_utc/%s_window_end_utc as "HH:MM" (got start=%s end=%s)\n' \
                "$file" "$prefix" "$prefix" "${start:-unset}" "${end:-unset}" >&2
            return 2
        fi
        slot=""
        hour="$(_backup_window_yaml_value "pg_${prefix}_cron_hour" "$file")"
        minute="$(_backup_window_yaml_value "pg_${prefix}_cron_minute" "$file")"
        if [[ "$hour" =~ ^[0-9]{1,2}$ && "$minute" =~ ^[0-9]{1,2}$ ]]; then
            slot="$(printf '%02d:%02d\n' "$((10#$hour))" "$((10#$minute))")"
        fi
        printf '%s %s %s\n' "$start" "$end" "$slot"
    done < <(awk '
        $1 ~ /^[a-z_][a-z0-9_]*_window_start_utc:$/ {
            sub(/:$/, "", $1); sub(/_window_start_utc$/, "", $1); print $1
        }' "$file")
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

# ── the live check (gr511981) ────────────────────────────────────────────
# One pg_stat_activity probe through the repo's own scripts/prod-psql --ro.
# Echoes one line per running backup backend —
#   pid|application_name|backend_type|state|age
# — empty when none runs. Return 0 when the query ran (empty output = clear),
# 2 when it could not run (no prod-psql in the tree, ssh/psql down) — the
# caller then falls back to the fixed window.
#
# agent_ro lacks pg_read_all_stats, so another role's `query` text reads
# '<insufficient privilege>' — but application_name and backend_type are
# visible for every session, and pg_dump/pg_basebackup name themselves there
# (a walsender is what a base backup looks like). The query regex stays as a
# third signal: same-role probes do see the text.
backup_window_live_backups() {
    local root="${1:-$PWD}" psql out
    psql="${root}/scripts/prod-psql"
    [[ -x "$psql" ]] || return 2
    out="$(PRECIS_PROD_PSQL_OPTS="-At" "$psql" --ro "$_BACKUP_WINDOW_LIVE_SQL" 2>/dev/null)" || return 2
    printf '%s\n' "$out" | grep -E '^[0-9]+\|' || true
}

# Kept single-line for the psql invocation; --ro wraps it in BEGIN READ ONLY.
_BACKUP_WINDOW_LIVE_SQL="SELECT a.pid || '|' || coalesce(nullif(a.application_name,''),'-') || '|' || coalesce(a.backend_type,'-') || '|' || coalesce(a.state,'?') || '|' || coalesce(age(clock_timestamp(), coalesce(a.query_start, a.backend_start))::text,'?') FROM pg_stat_activity a WHERE a.pid <> pg_backend_pid() AND (a.application_name ~ 'pg_dump|pg_basebackup' OR a.backend_type = 'walsender' OR a.query ~* 'pg_dump|pg_basebackup') ORDER BY a.query_start NULLS LAST"

# The decision, with every input explicit so a test can fake the clock, the
# pending set and the live probe:
#   backup_window_decide <start> <end> <now> <pending> <ignore> [live] [slot] [detail]
# <pending> is the newline-separated file list, "" for none, or the literal
# word `unknown`. <ignore> is 1 for --ignore-backup-window. <live> is the
# probe verdict — `running`, `clear`, or `unavailable` (the default, for
# callers that never probed); <slot> is the pg_dump cron slot as HH:MM (""
# when unreadable — treated as still ahead); <detail> is the probe's line(s)
# naming what runs. Prints the refusal/warning/override note; return codes
# as in the header.
backup_window_decide() {
    local start="$1" end="$2" now="$3" pending="$4" ignore="${5:-0}" \
        live="${6:-unavailable}" slot="${7:-}" detail="${8:-}" n list
    local in_window=0
    backup_window_contains "$start" "$end" "$now" && in_window=1

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
        # No migration at stake: a running dump cannot block a code-only
        # deploy, and the guard never probed on this path.
        (( in_window == 0 )) && return 0
        printf 'WARNING: inside the %s–%s UTC backup window (now %s UTC). No pending migration, so proceeding — but the scheduled pg_dump for this leg runs inside it and (in the early-morning leg) the DB node reboots too; expect dropped connections. Window ends %s UTC.\n' \
            "$start" "$end" "$now" "$end"
        return 0
    fi

    if [[ "$ignore" == 1 ]]; then
        if [[ "$live" == running ]]; then
            printf 'NOTE: --ignore-backup-window — proceeding on operator override even though a backup IS running on prod right now:\n%s\nThe migration task will block behind it until it exits.\n' \
                "$(printf '%s\n' "$detail" | sed 's/^/    /')"
        else
            printf 'NOTE: --ignore-backup-window — inside the %s–%s UTC backup window (now %s UTC) with %s pending migration(s); proceeding on operator override. If pg_dump is running, the migration task will block until it finishes (~%s UTC).\n' \
                "$start" "$end" "$now" "$n" "$end"
            [[ -z "$list" ]] || printf '%s\n' "$list"
        fi
        return 0
    fi

    # A backup live on prod refuses at ANY hour — the r91 deploy resumed at
    # 04:22Z, outside the window, mid-dump (gr511981).
    if [[ "$live" == running ]]; then
        printf 'REFUSING TO DEPLOY: a backup is running on the prod DB right now — pg_stat_activity shows:\n%s\npg_dump holds ACCESS SHARE on every table it has read, so the migration'"'"'s DDL would block behind it and the deploy could die mid-apply, leaving the DB part-migrated (gr511981). Re-run when the backup exits, or — if this backend is not the nightly dump — re-run with --ignore-backup-window. docs/runbooks/migration-deploy-window.md.\n' \
            "$(printf '%s\n' "$detail" | sed 's/^/    /')"
        return 1
    fi

    # Outside the window a clear or unrunnable probe proceeds: the live check
    # only ever adds refusals out here.
    (( in_window == 0 )) && return 0

    # Inside the window. A 'clear' probe overrides the clock only once the
    # scheduled slot has passed — before it, nothing running now says nothing
    # about the dump that fires at <slot>.
    if [[ "$live" == clear && "$slot" =~ ^[0-9]{2}:[0-9]{2}$ ]] \
        && (( $(_backup_window_minutes "$now") >= $(_backup_window_minutes "$slot") )); then
        printf 'NOTE: live check — no pg_dump/pg_basebackup backend on the prod DB (pg_stat_activity) and the %s UTC cron slot has passed (now %s UTC), so tonight'"'"'s dump finished or never ran. Proceeding with %s pending migration(s).\n' \
            "$slot" "$now" "$n"
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
    case "$live" in
        clear)
            printf 'The live check found no pg_dump/pg_basebackup running, but the scheduled %s UTC dump is still ahead — a clear probe cannot clear a run that has not started.\n' "${slot:-$start}" ;;
        *)
            printf 'The live pg_stat_activity check could not run (prod-psql missing or prod unreachable), so the fixed window decides.\n' ;;
    esac
    # shellcheck disable=SC2016  # the `ps ... | grep pg_dump` text is the
    # literal advice printed to the operator, not an expansion
    printf 'The nightly pg_dump holds ACCESS SHARE on every table, so the migration'"'"'s DDL would block behind it and the deploy would hang with a silent log (2026-09-08). Aborting mid-deploy is worse than waiting. Wait until the window ends at %s UTC, or — after checking the dump is not running (`ps -eo pid,etime,args | grep pg_dump` on the DB node) — re-run with --ignore-backup-window. docs/runbooks/migration-deploy-window.md.\n' "$end"
    return 1
}

# The whole guard for scripts/deploy: bounds from the tree, clock from
# DEPLOY_NOW_UTC/date, pending set from git, live probe from prod-psql.
#   backup_window_guard <repo_root> <target> [ignore] [prod_ref]
backup_window_guard() {
    local root="$1" target="$2" ignore="${3:-0}" prod_ref="${4:-origin/prod}" \
        windows start="" end="" slot="" now pending live detail wstart wend wslot
    windows="$(backup_window_windows "$root")" || return 2
    now="$(backup_window_now)" || return 2
    # Decide against the window holding the clock — its bounds and cron slot
    # are what the refusal/warning names. Outside every window the FIRST
    # (primary) window's values are passed through; decide() returns early on
    # in_window=0, so they only surface in the operator-override note.
    while read -r wstart wend wslot; do
        if [[ -z "$start" ]]; then
            start="$wstart" end="$wend" slot="$wslot"
        fi
        if backup_window_contains "$wstart" "$wend" "$now"; then
            start="$wstart" end="$wend" slot="$wslot"
            break
        fi
    done <<< "$windows"
    [[ -n "$start" ]] || {
        printf 'backup-window: no *_window_*_utc pair in %s — cannot read the window bounds\n' \
            "$(backup_window_defaults_file "$root")" >&2
        return 2
    }
    # The pending set is needed at ANY hour now (the live probe can refuse
    # outside the window); still offline git work either way. The probe —
    # the only prod round-trip — runs only when a migration may be in play.
    if ! pending="$(deploy_pending_migrations "$root" "$target" "$prod_ref")"; then
        pending=unknown
    fi
    live=unavailable
    detail=""
    if [[ "$pending" == unknown || -n "$pending" ]]; then
        if detail="$(backup_window_live_backups "$root")"; then
            if [[ -n "$detail" ]]; then live=running; else live=clear; fi
        fi
    fi
    backup_window_decide "$start" "$end" "$now" "$pending" "$ignore" "$live" "$slot" "$detail"
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

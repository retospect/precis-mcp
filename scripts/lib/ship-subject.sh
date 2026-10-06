#!/usr/bin/env bash
# Squash-commit message resolution + lint for scripts/ship (sourced).
#
# The landed subject is what `git log` shows forever, so it must say WHAT
# changed — never a `ship(<branch>): squash-merge` or `wip(...)` placeholder.
#
#   ship_subject_lint SUBJECT      → 0 ok; 1 + one-line reason on stderr
#   ship_resolve_msg MSG BASE_REF  → resolved message on stdout (subject, optional
#                                    body, no trailers); 1 + reason on stderr
#
# Resolution: MSG (-m) wins; else the branch's non-WIP, non-merge commits since
# BASE_REF: one → its full message minus trailers; several → the newest's subject
# plus an "Also:" list of the rest; none → refuse. Both fail before any gate.
# PRECIS_SHIP_SUBJECT_LINT=0 skips the lint (emergencies only).

_SHIP_SUBJECT_RE='^(feat|fix|docs|test|chore|build|refactor|perf|ci|release|revert)(\([a-z0-9/_.-]+\))?!?: [^[:space:]]'
_SHIP_TRAILER_RE='^(Co-Authored-By|Claude-Session|Gate|Signed-off-by):'

ship_subject_lint() {
    local subject="${1%%$'\n'*}"
    [[ "${PRECIS_SHIP_SUBJECT_LINT:-1}" == 0 ]] && return 0
    if [[ ! "$subject" =~ $_SHIP_SUBJECT_RE ]]; then
        printf "commit subject must look like 'type(scope): what changed' (feat|fix|docs|test|chore|build|refactor|perf|ci|release|revert) — got: %s — fix it, or set PRECIS_SHIP_SUBJECT_LINT=0 in an emergency\n" "$subject" >&2
        return 1
    fi
    if (( ${#subject} > 72 )); then
        printf "commit subject is %d chars, max 72 — shorten it and put detail in the body — got: %s — or set PRECIS_SHIP_SUBJECT_LINT=0 in an emergency\n" "${#subject}" "$subject" >&2
        return 1
    fi
    return 0
}

ship_resolve_msg() {
    local msg="${1:-}" base="${2:-origin/main}" sha subj out
    local -a shas=()
    if [[ -z "$msg" ]]; then
        while IFS= read -r sha; do
            [[ -n "$sha" ]] || continue
            subj="$(git log -1 --format=%s "$sha")"
            [[ "$subj" == wip\(* ]] && continue
            shas+=("$sha")
        done < <(git rev-list --no-merges "${base}..HEAD" 2>/dev/null)
        case "${#shas[@]}" in
            0)
                echo "no commit message: pass -m 'type(scope): what changed' or commit your work with a real subject before shipping" >&2
                return 1 ;;
            1)
                msg="$(git log -1 --format=%B "${shas[0]}")" ;;
            *)
                msg="$(git log -1 --format=%s "${shas[0]}")"$'\n\nAlso:'
                for sha in "${shas[@]:1}"; do
                    msg+=$'\n'"- $(git log -1 --format=%s "$sha")"
                done ;;
        esac
    fi
    # Drop trailer lines (ship re-adds its own); $(...) trims trailing blanks.
    out="$(printf '%s\n' "$msg" | grep -Ev "$_SHIP_TRAILER_RE" || true)"
    ship_subject_lint "$out" || return 1
    printf '%s\n' "$out"
}

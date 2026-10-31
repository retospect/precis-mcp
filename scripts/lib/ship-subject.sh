#!/usr/bin/env bash
# bash 3.2 floor (stock macOS): no declare -A, mapfile, ${x,,}, &>, |&, coproc, globstar — tests/test_bash32_gotchas.py
# Squash-commit message resolution + lint for scripts/ship (sourced).
#
# The landed subject is what `git log` shows forever, so it must say WHAT
# changed — never a `ship(<branch>): squash-merge` or `wip(...)` placeholder.
#
#   ship_subject_lint SUBJECT      → 0 ok; 1 + one-line reason on stderr
#   ship_resolve_msg MSG BASE_REF  → resolved message on stdout (subject, optional
#                                    body, trailer block kept); 1 + reason on stderr
#   ship_msg_body                  → stdin message minus its trailer block
#   ship_mint_trailers GATE CO [EXTRA]
#                                  → stdin message's trailer block minus Gate:,
#                                    $1 prepended when set, $3's lines merged
#                                    after it (dedup, first occurrence wins),
#                                    $2 appended only when neither the message
#                                    nor $3 carries a Co-Authored-By
#   ship_branch_attr_trailers BASE [HEAD]
#                                  → the branch's Co-Authored-By/Claude-Session
#                                    trailers (BASE..HEAD, no merges, no wip(*),
#                                    deduped, oldest commit first)
#
# Resolution: MSG (-m) wins; else the branch's non-WIP, non-merge commits since
# BASE_REF: one → its full message; several → refuse when their
# type(scope) prefixes disagree (a fix-up test commit must not label a fix; gr495447),
# else the OLDEST subject plus an "Also:" list of the rest; none → refuse. All
# fail before any gate.
# PRECIS_SHIP_SUBJECT_LINT=0 skips the lint (emergencies only).
#
# Trailers stay verbatim through the squash (gr517803): a non-Claude
# builder's Co-Authored-By/Claude-Session lands as written — they used to
# be dropped and a Claude footer substituted. The mint merges them with
# ship's own Gate:/fallback-Co-Authored-By instead.

_SHIP_SUBJECT_RE='^(feat|fix|docs|test|chore|build|refactor|perf|ci|release|revert)(\([a-z0-9/_.-]+\))?!?: [^[:space:]]'

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
    local msg="${1:-}" base="${2:-origin/main}" sha subj
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
                # rev-list is newest first: the oldest commit is the last element.
                local last=$(( ${#shas[@]} - 1 )) prefix first_prefix="" mixed=0 i
                local -a subjects=()
                for sha in ${shas[@]+"${shas[@]}"}; do
                    subj="$(git log -1 --format=%s "$sha")"
                    subjects+=("$subj")
                    prefix="${subj%%:*}"
                    first_prefix="${first_prefix:-$prefix}"
                    [[ "$prefix" == "$first_prefix" ]] || mixed=1
                done
                if (( mixed )) && [[ "${PRECIS_SHIP_SUBJECT_LINT:-1}" != 0 ]]; then
                    printf "%d commits with different type(scope) prefixes, so the squash subject would mislabel the change (a fix-up commit would win) — pass -m '<type(scope): what changed>'. Subjects:\n" "${#shas[@]}" >&2
                    printf '  - %s\n' "${subjects[@]}" >&2
                    return 1
                fi
                msg="${subjects[$last]}"$'\n\nAlso:'
                for (( i = last - 1; i >= 0; i-- )); do
                    msg+=$'\n'"- ${subjects[$i]}"
                done
                # The composed message is subjects only — carry the commits'
                # trailer blocks over too (deduped), or a multi-commit Devin/
                # Codex branch would lose its footer the same way (gr517803).
                # Gate: is ship-minted — a stale one is never carried.
                local _kept
                _kept="$(
                    for sha in ${shas[@]+"${shas[@]}"}; do
                        git log -1 --format=%B "$sha" | git interpret-trailers --parse
                    done | grep -v '^Gate:' | awk '!seen[$0]++'
                )"
                [[ -n "$_kept" ]] && msg="${msg}"$'\n\n'"${_kept}" ;;
        esac
    fi
    # The trailer block rides along verbatim — the mint splits it off with
    # ship_msg_body and re-emits it through ship_mint_trailers. $(...) trims
    # trailing blanks.
    ship_subject_lint "$msg" || return 1
    printf '%s\n' "$msg"
}

# ship_branch_attr_trailers BASE [HEAD]: the branch commits' Co-Authored-By/
# Claude-Session trailers, deduped, oldest-commit-first. The resolved
# message does not always carry them — an explicit -m never consulted the
# branch (gr538832: the squash then minted only the generic Co-Authored-By
# and the branch's real footer never reached main). wip(*) snapshots are
# skipped the same way ship_resolve_msg skips them (a footer ship minted
# itself is not attribution), and HEAD is bounded to the pre-WIP tip for
# the same reason — under -m the snapshot commit is named after -m, not
# wip(*). Collect BEFORE _squash_and_rebase / an in-lock forward merge
# rewrites HEAD — afterwards base..HEAD includes other people's commits.
ship_branch_attr_trailers() {
    local sha subj head="${2:-HEAD}"
    while IFS= read -r sha; do
        [[ -n "$sha" ]] || continue
        subj="$(git log -1 --format=%s "$sha")"
        [[ "$subj" == wip\(* ]] && continue
        git log -1 --format=%B "$sha" | git interpret-trailers --parse
    done < <(git rev-list --reverse --no-merges "${1:-origin/main}..$head" 2>/dev/null) \
        | awk 'tolower($0) ~ /^(co-authored-by|claude-session):/ && !seen[$0]++'
}

# ship_msg_body: stdin message → stdout, minus the trailer block. What
# counts as the block is git's own rule (interpret-trailers): the last
# paragraph, every line `Key: value` (continuation lines ride along), and
# not the whole message — so a lone "fix: x" subject is never eaten.
ship_msg_body() {
    awk '
        { l[NR] = $0 }
        END {
            e = NR
            while (e > 0 && l[e] ~ /^[[:space:]]*$/) e--
            s = e
            while (s > 0 && l[s] ~ /[^[:space:]]/) s--
            strip = 0
            if (s >= 1 && s < e) {
                strip = 1; seen = 0
                for (i = s + 1; i <= e; i++) {
                    if (l[i] ~ /^[A-Za-z][A-Za-z0-9-]*:/) { seen = 1; continue }
                    if (l[i] ~ /^[ \t]/) continue
                    strip = 0; break
                }
                if (!seen) strip = 0
            }
            n = strip ? s - 1 : NR
            for (i = 1; i <= n; i++) print l[i]
        }'
}

# ship_mint_trailers: stdin resolved message → stdout, the trailer block for
# the minted commit: the branch's own trailers verbatim except a stale
# Gate: (ship-owned, minted fresh), $1 prepended when set, $3 — extra
# trailers, the branch commits' attribution — merged in after them (deduped,
# first occurrence wins; covers the -m path whose resolved message has no
# trailer block at all, gr538832), and $2 — the default Co-Authored-By —
# appended ONLY when neither the message nor $3 carries a Co-Authored-By
# (gr517803: a Codex/Devin footer must not be shadowed by a substituted
# Claude one).
ship_mint_trailers() {
    local msg kept extra="${1:-}" fallback="${2:-}" attrs="${3:-}"
    msg="$(cat)"
    kept="$(printf '%s\n' "$msg" | git interpret-trailers --parse 2>/dev/null | grep -v '^Gate:' || true)"
    [[ -n "$extra" ]] && kept="${extra}${kept:+$'\n'}${kept}"
    if [[ -n "$attrs" ]]; then
        kept="$(printf '%s\n%s\n' "$kept" "$attrs" | awk 'NF && !seen[$0]++')"
    fi
    if [[ -n "$fallback" ]] && ! printf '%s\n%s\n' "$msg" "$attrs" | grep -qi '^Co-Authored-By:'; then
        kept="${kept:+$kept$'\n'}${fallback}"
    fi
    printf '%s\n' "$kept"
}

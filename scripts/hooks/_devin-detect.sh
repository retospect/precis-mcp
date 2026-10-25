# shellcheck shell=bash
# scripts/hooks/_devin-detect.sh — the ONE hook-runtime detector
# (gr515490). Sourced, never executed. Why one copy: Devin's
# pre_compact contract wants stdout EMPTY or valid JSON, so every
# stdout-printing hook needs the same answer — a drifted per-hook copy
# is what reddened the Devin panes twice (gr513748, gr514171).
#
# Markers: Devin exports no AI_AGENT (the string never appears in the
# devin binary, gr515490) and its pre-first-prompt payloads lack
# prompt_id/turn_id (auto-compact on a resumed session, gr514171) — so
# DEVIN_PROJECT_DIR, exported for every Devin hook spawn, is the
# reliable marker. The payload ids and AI_AGENT stay as fallbacks for
# the shapes that do carry them.
#
#   hook_is_devin [payload] — returns 0 under the Devin CLI. Pass the
#     hook's stdin payload when the script already read it.
#   hook_project_dir        — prints the workspace root the harness
#     exported for this spawn (DEVIN_PROJECT_DIR, else
#     CLAUDE_PROJECT_DIR); empty when neither is set.
hook_is_devin() {
    case "${1:-}" in
        *'"prompt_id"'* | *'"turn_id"'*) return 0 ;;
    esac
    case "${AI_AGENT:-}" in
        devin*) return 0 ;;
    esac
    [ -n "${DEVIN_PROJECT_DIR:-}" ]
}

hook_project_dir() {
    printf '%s' "${DEVIN_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-}}"
}

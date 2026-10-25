#!/usr/bin/env bash
# scripts/lib/pytest-inner.sh — the in-container half of `scripts/test`.
#
# Invoked as `bash scripts/lib/pytest-inner.sh <pytest args…>` inside
# `uv run --no-sync ${UV_WITH}` in a throwaway precis-dev container, with
# the worktree bind-mounted at /app. Runs the three stale-baked-venv
# preflight checks (see the Preflight comment in scripts/test), decides
# -n0/-n6 when PRECIS_AUTO_N=1 (test-loop-speed Decision 1: count the
# collected selection, serial at ≤PRECIS_AUTO_N_MAX=500), then execs
# pytest. -n flags already in "$@" (caller-supplied or host-decided)
# pass through untouched.
set -uo pipefail

python3 scripts/lib/prewarm-pycache.py pysbd || exit $?
# S2 (gr514478): the three stale-baked-venv checks guard a real failure
# (gr286507, gr453828, gr494988) but cost ~6 s every run — cache their
# pass. The host computes PRECIS_PREFLIGHT_KEY from image id + uv.lock +
# pyproject.toml + UV_WITH; a stamp file at the worktree root records the
# last passing key. A check that ran under a different key, a failure,
# and an entry-points heal (which patched only this throwaway container,
# marked by "precis-preflight: healed") all leave no stamp, so the next
# run checks again. No key → checks run every time, same as before.
PREFLIGHT_STAMP="${PRECIS_PREFLIGHT_STAMP:-/app/.precis-preflight}"
if [[ -n "${PRECIS_PREFLIGHT_KEY:-}" && -f "$PREFLIGHT_STAMP" ]] \
    && [[ "$(cat "$PREFLIGHT_STAMP" 2>/dev/null)" == "$PRECIS_PREFLIGHT_KEY" ]]; then
    : # stamp hit — the checks already passed under this exact key
else
    python3 scripts/lib/check-core-deps.py || exit $?
    python3 scripts/lib/check-lock-pins.py || exit $?
    _ep_out="$(python3 scripts/lib/check-entry-points.py --heal 2>&1)" || {
        printf '%s\n' "${_ep_out:-}" >&2
        exit 1
    }
    if [[ -n "$_ep_out" ]]; then
        printf '%s\n' "$_ep_out" >&2
    fi
    if [[ -n "${PRECIS_PREFLIGHT_KEY:-}" && "$_ep_out" != *"precis-preflight: healed"* ]]; then
        printf '%s\n' "$PRECIS_PREFLIGHT_KEY" >"$PREFLIGHT_STAMP" 2>/dev/null || true
    fi
fi

NP_ARGS=()
if [[ "${PRECIS_AUTO_N:-0}" == "1" ]]; then
    count="$(python3 scripts/lib/collect_count.py "$@")" || count=""
    if [[ "$count" =~ ^[0-9]+$ && "$count" -le "${PRECIS_AUTO_N_MAX:-500}" ]]; then
        printf 'small selection: -n0 (%s tests collected; pass -nN to override)\n' \
            "$count"
        NP_ARGS=(-n0)
    else
        NP_ARGS=(-n6)
    fi
fi

# -p no:warnings drops the warnings summary (noise while iterating);
# --tb=short keeps tracebacks compact; the -q comes from scripts/test
# (VERBOSITY) so a caller-supplied -v/-q can replace it.
exec python3 -P -m pytest --no-header --tb=short -p no:warnings \
    ${NP_ARGS[@]+"${NP_ARGS[@]}"} "$@"

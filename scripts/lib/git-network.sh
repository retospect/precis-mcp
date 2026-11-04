# shellcheck shell=bash
# Shared diagnostics and bounded, ref-checked push retries live in Python so
# shell and Python release callers classify the same failures the same way.
_GIT_NETWORK_HELPER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/git_network.py"
# Formerly unbounded shell calls allow degraded remote service to recover.
# Callers that already had tighter limits use git_network_bounded explicitly.
GIT_NETWORK_SHELL_TIMEOUT=300

git_network() {
    git_network_bounded "$GIT_NETWORK_SHELL_TIMEOUT" "$@"
}

git_network_bounded() {
    local timeout="$1"; shift
    python3 "$_GIT_NETWORK_HELPER" --timeout "$timeout" -- "$@"
}

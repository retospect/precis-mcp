# shellcheck shell=bash
# bash 3.2 floor (stock macOS): no declare -A, mapfile, ${x,,}, &>, |&, coproc, globstar — tests/test_bash32_gotchas.py
# Shared mechanics for scripts/kimi-tree and scripts/devin-tree (sourced).
#
# A launcher sets `at_label` (its tool name: error prefix `<label>-tree:` and
# the tmux window name `<label>-NAME`) before sourcing, then calls at_init,
# at_check_name, at_resolve_tmux, at_ensure_tree and at_launch / at_exec_relock.
# Each launcher keeps its own flag parsing, setup step, env and the final
# `exec <tool> args`.

at_label=${at_label:?set at_label before sourcing agent-tree.sh}

at_init() {
	at_here=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)  # the scripts dir
	# git wins (a launcher run from inside a worktree must still target the
	# main checkout's .claude/worktrees); outside any repo — the test gate
	# copies the tree without .git — fall back to the checkout root (the
	# scripts dir's parent) so argument validation and --dry-run stay
	# repo-independent (gr531394).
	at_root=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || at_root=""
	at_root=${at_root%/.git}
	[[ -n $at_root ]] || at_root=$(cd "$at_here/.." && pwd -P)
}

at_check_name() {
	[[ $1 =~ ^[a-z0-9][a-z0-9._-]*$ ]] || { echo "$at_label-tree: bad name: $1" >&2; exit 2; }
}

# at_resolve_tmux SESSION: echo SESSION, or the current tmux session if empty.
at_resolve_tmux() {
	if [[ -n $1 ]]; then echo "$1"; return; fi
	[[ -n ${TMUX:-} ]] || { echo "$at_label-tree: not in tmux; name a session: --tmux SESSION" >&2; exit 2; }
	tmux display-message -p '#S'
}

# at_lock TREE: hold the tree under this pid until a --exec re-locks it to the
# tool's pid, so the reaper never sees a fresh, merged, clean, sessionless tree.
at_lock() {
	git -C "$at_root" worktree unlock "$1" >/dev/null 2>&1 || true
	git -C "$at_root" worktree lock --reason "pid $$" "$1"
}

# at_ensure_tree NAME TREE BRANCH PURPOSE: create-or-reuse the worktree from
# origin/main (existing tree/branch reused, never reset), seed .venv, lock,
# write .claude/purpose.
at_ensure_tree() {
	local name=$1 tree=$2 branch=$3 purpose=$4
	if [[ ! -d $tree ]]; then
		git -C "$at_root" fetch -q origin main
		if git -C "$at_root" show-ref -q --verify "refs/heads/$branch"; then
			git -C "$at_root" worktree add -q "$tree" "$branch"
		else
			git -C "$at_root" worktree add -q -b "$branch" "$tree" origin/main
		fi
	fi
	# A failed seed never blocks the launch (gr514738).
	"$at_here/seed-worktree-venv" "$tree" "$at_root" \
		|| echo "$at_label-tree: venv seed failed; first uv run will build it" >&2
	at_lock "$tree"
	[[ -n $purpose ]] && printf '%s\n' "$purpose" >"$tree/.claude/purpose"
	return 0
}

# at_exec_relock TREE: the `--exec` half. Re-lock TREE to this pid and cd in;
# the launcher then sets its env and execs the tool under the same pid.
at_exec_relock() {
	at_lock "$1"
	cd "$1" || exit 1
}

# at_launch SELF NAME TREE USE_TMUX SESSION [TOOL ARGS...]: run the tool in
# this terminal (exec into SELF --exec) or in a new tmux window, then wait
# (<=10 s) for the window to take the lock over before this pid exits.
at_launch() {
	local self=$1 name=$2 tree=$3 use_tmux=$4 session=$5
	shift 5
	if ((!use_tmux)); then
		exec "$self" --exec "$tree" "$@"
	fi
	tmux new-window -t "=$session:" -n "$at_label-$name" -c "$tree" \
		"exec $(printf '%q ' "$self" --exec "$tree" "$@")"
	local _ reason list
	for _ in $(seq 50); do
		# Capture, then scan: an early-exit awk reading a live git pipe can
		# SIGPIPE git under pipefail and kill the launch mid-flight
		# (gr543576 — the 141 flake in test_launch_registers_trust).
		list=$(git -C "$at_root" worktree list --porcelain)
		reason=$(awk -v t="worktree $tree" \
			'$0==t{f=1} f&&/^locked/{print; exit} f&&/^$/{exit}' <<<"$list")
		[[ $reason != "locked pid $$" ]] && break
		sleep 0.2
	done
	echo "$at_label-tree: $name → $tree (tmux $session:$at_label-$name)"
}

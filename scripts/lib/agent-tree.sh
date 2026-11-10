# shellcheck shell=bash
# bash 3.2 floor (stock macOS): no declare -A, mapfile, ${x,,}, &>, |&, coproc, globstar — tests/test_bash32_gotchas.py
# Shared mechanics for scripts/agent-start and the agent-specific entries.
#
# A launcher sets `at_label` (its tool name: error prefix `<label>-tree:` and
# the tmux window name `<label>-NAME`) before sourcing, then calls at_init,
# at_check_name, at_resolve_tmux, at_ensure_tree and at_launch / at_exec_relock.
# Each launcher keeps its own flag parsing, setup step, env and the final
# `exec <tool> args`.

at_label=${at_label:?set at_label before sourcing agent-tree.sh}

at_init() {
	at_here=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)  # the scripts dir
	# Resolve the launcher's repository, never the caller's unrelated cwd.
	# A launcher inside a worktree still targets main's .claude/worktrees;
	# outside any repo — the test gate
	# copies the tree without .git — fall back to the checkout root (the
	# scripts dir's parent) so argument validation and --dry-run stay
	# repo-independent (gr531394).
	at_root=$(git -C "$at_here/.." rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || at_root=""
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
	local list reason holder handoff=${2:-}
	list=$(git -C "$at_root" worktree list --porcelain)
	reason=$(awk -v t="worktree $1" '$0==t{f=1} f&&/^locked/{print; exit} f&&/^$/{exit}' <<<"$list")
	if [[ -n $reason ]]; then
		holder=${reason#locked pid }
		case $holder in
		''|*[!0-9]*) echo "$at_label-tree: refusing non-session lock on $1" >&2; return 1 ;;
		esac
		[[ $holder == "$$" ]] && return 0
		if [[ $holder != "$handoff" ]] && { kill -0 "$holder" 2>/dev/null || ps -p "$holder" -o pid= 2>/dev/null | grep -q '[0-9]'; }; then
			echo "$at_label-tree: worktree held by live pid $holder: $1" >&2
			return 1
		fi
		git -C "$at_root" worktree unlock "$1"
	fi
	git -C "$at_root" worktree lock --reason "pid $$" "$1"
}

# Never launch in an empty directory, a different repository/branch, or a
# sparse checkout. Dirty work is preserved; at least one tracked file must
# actually exist on disk. A missing tree needs explicit recovery, not reset.
at_validate_tree() {
	local tree=$1 branch=$2 top common list tracked path found=0
	top=$(git -C "$tree" rev-parse --show-toplevel) || return 1
	common=$(git -C "$tree" rev-parse --path-format=absolute --git-common-dir) || return 1
	list=$(git -C "$at_root" worktree list --porcelain)
	if [[ $top != "$tree" || $common != "$at_root/.git" ]] ||
		! grep -Fqx "worktree $tree" <<<"$list" ||
		[[ $(git -C "$tree" symbolic-ref --quiet --short HEAD) != "$branch" ]] ||
		[[ $(git -C "$tree" config --bool core.sparseCheckout || true) == true ]]; then
		echo "$at_label-tree: not the expected full worktree: $tree ($branch)" >&2
		return 1
	fi
	tracked=$(git -C "$tree" ls-files)
	while IFS= read -r path; do
		if [[ -n $path && ( -e $tree/$path || -L $tree/$path ) ]]; then found=1; break; fi
	done <<<"$tracked"
	if (( !found )); then
		echo "$at_label-tree: no checked-out tracked files in $tree; recover it before launching" >&2
		return 1
	fi
}

# at_ensure_tree NAME TREE BRANCH PURPOSE: create-or-reuse the worktree from
# origin/main (existing tree/branch reused, never reset), seed .venv, lock,
# write .claude/purpose.
at_ensure_tree() {
	local name=$1 tree=$2 branch=$3 purpose=$4
	if [[ ! -d $tree ]]; then
		git -C "$at_root" fetch -q origin main
		if git -C "$at_root" show-ref -q --verify "refs/heads/$branch"; then
			git -C "$at_root" worktree add --lock --reason "pid $$" -q "$tree" "$branch"
		else
			git -C "$at_root" worktree add --lock --reason "pid $$" -q -b "$branch" "$tree" origin/main
		fi
	fi
	at_validate_tree "$tree" "$branch"
	at_lock "$tree"
	# A failed seed never blocks the launch (gr514738).
	"$at_here/seed-worktree-venv" "$tree" "$at_root" \
		|| echo "$at_label-tree: venv seed failed; first uv run will build it" >&2
	mkdir -p "$tree/.claude"
	[[ -n $purpose ]] && printf '%s\n' "$purpose" >"$tree/.claude/purpose"
	return 0
}

# at_exec_relock TREE: the `--exec` half. Re-lock TREE to this pid and cd in;
# the launcher then sets its env and execs the tool under the same pid.
at_exec_relock() {
	local handoff=${AT_HANDOFF_PID:-}
	unset AT_HANDOFF_PID
	at_validate_tree "$1" "worktree-${1##*/}"
	at_lock "$1" "$handoff"
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
		"AT_HANDOFF_PID=$$ exec $(printf '%q ' "$self" --exec "$tree" "$@")"
	local _ reason list
	for _ in $(seq 50); do
		# Capture, then scan: an early-exit awk reading a live git pipe can
		# SIGPIPE git under pipefail and kill the launch mid-flight
		# (gr543576 — the 141 flake in test_launch_registers_trust).
		list=$(git -C "$at_root" worktree list --porcelain)
		reason=$(awk -v t="worktree $tree" \
			'$0==t{f=1} f&&/^locked/{print; exit} f&&/^$/{exit}' <<<"$list")
		[[ -n $reason && $reason != "locked pid $$" ]] && break
		sleep 0.2
	done
	if [[ -z $reason || $reason == "locked pid $$" ]]; then
		echo "$at_label-tree: tmux session did not take over its worktree lock: $tree" >&2
		return 1
	fi
	echo "$at_label-tree: $name → $tree (tmux $session:$at_label-$name)"
}

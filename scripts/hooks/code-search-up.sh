#!/usr/bin/env bash
# SessionStart hook: orient a session (including a fresh `claude -w` worktree)
# on where code search reads from and where the shell operates.
#
# Code discovery is the precis session MCP's python kind, whose `main::` alias
# is a read-only live mount of the MAIN checkout (not the caller's worktree).
# Nothing to index or start per worktree. This hook only prints the orientation
# lines — the MAIN-vs-worktree split and the exact structural tool — and must
# never block or fail session start.
#
# Wired in .claude/settings.json (SessionStart).
set -euo pipefail
cd "$(dirname "$0")/../.."

# The MAIN checkout (the parent of the shared .git).
MAIN_ROOT="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)")" || MAIN_ROOT=""

# Where the shell/Read/Edit actually operate this session — the current
# checkout's toplevel. In a `claude -w` worktree this differs from MAIN_ROOT;
# surfacing it (below) keeps the worktree path as available as the main one, so
# a `cd <main-repo>` reflex doesn't split the two checkouts (guard-cd-to-primary).
WORKTREE_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || WORKTREE_ROOT=""

if [[ -n "$MAIN_ROOT" ]]; then
    echo "🔎 code search (precis python kind): get(kind='python', id='main::<qualname>') — reads MAIN, not this worktree; Grep is truth for worktree changes."
    if [[ -n "$WORKTREE_ROOT" && "$(git rev-parse --git-dir 2>/dev/null)" != "$(git rev-parse --git-common-dir 2>/dev/null)" ]]; then
        echo "   your own tree (linked worktree): get(kind='python', id='wt-$(basename "$WORKTREE_ROOT")::<qualname>') — read-only; Grep if it doesn't resolve."
    fi
    echo "   find symbols: search(kind='python', mode='pattern', q='<qualname regex>'); skill precis-python-help."
    if [[ -n "$WORKTREE_ROOT" && "$WORKTREE_ROOT" != "$MAIN_ROOT" ]]; then
        echo "   ⚠ shell/Read/Edit operate in THIS worktree: $WORKTREE_ROOT"
        echo "     Run Bash bare (cwd is already here); never 'cd' to the MAIN path ($MAIN_ROOT)."
        echo "     Other trees: a -C redirect is refused by the harness — read them with scripts/inflight --json."
    fi
    echo "🧭 exact who-calls / what-depends-on (Python): scripts/coderef callers|deps <file.py::Sym>"
    echo "   (structural, deterministic — prefer over grepping a bare symbol name)."
fi
exit 0

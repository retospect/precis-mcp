#!/usr/bin/env bash
# SessionStart hook: make the code-search index usable the moment a session
# (including a fresh `claude -w` worktree) comes up — WITHOUT indexing anything
# per-worktree.
#
# The claude-context collection is keyed to the *absolute path* of what was
# indexed, and stores repo-RELATIVE paths inside. So a single index of the MAIN
# checkout is the shared index: every worktree reuses it by searching with the
# main path, and each hit (`src/precis/foo.py`) maps straight onto the identical
# relative path in the worktree. Nothing to (re)index on worktree creation.
#
# All this hook does is (1) guarantee Milvus is reachable so the claude-context
# MCP can connect, and (2) print the one thing a session needs to know to hit
# the shared index. It must never block or fail session start — Milvus being
# down just means code search is unavailable this session, not a broken start.
#
# Seeding (once per machine): in any session with the claude-context MCP loaded,
#   index_codebase(path="<main-root>")
# Freshness is lazy: the synchronizer reconciles changed files by Merkle diff on
# next use — a merge or two of lag on a navigation aid costs nothing.
#
# Wired in .claude/settings.json (SessionStart). Rationale: memory
# `repo_dev_claude_tooling` + the "Semantic code search" convention in CLAUDE.md.
set -euo pipefail
cd "$(dirname "$0")/../.."

COMPOSE="docker/code-search/compose.yaml"
[[ -f "$COMPOSE" ]] || exit 0

# The shared index lives under the MAIN checkout's absolute path (the parent of
# the shared .git). Print it so the session searches the right collection.
MAIN_ROOT="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)")" || MAIN_ROOT=""

# Bring Milvus up if it isn't. `up -d` returns fast when already running; on a
# cold machine it boots 3 containers (images are pre-pulled). Silence + never
# fail: no docker / no daemon → code search is simply off this session.
if command -v docker >/dev/null 2>&1; then
    docker compose -f "$COMPOSE" up -d >/dev/null 2>&1 || true
fi

if [[ -n "$MAIN_ROOT" ]]; then
    echo "🔎 code search (claude-context MCP): shared MAIN index — call search_code with"
    echo "   path=\"$MAIN_ROOT\" (hits are repo-relative → they map onto this worktree)."
    echo "🧭 exact who-calls / what-depends-on (Python): scripts/coderef callers|deps <file.py::Sym>"
    echo "   (structural, deterministic — prefer over grepping a bare symbol name)."
fi
exit 0

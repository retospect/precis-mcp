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
# All this hook does is (1) guarantee Milvus + the bge-m3 embed shim are
# reachable so the claude-context MCP can connect and embed, and (2) print the
# one thing a session needs to know to hit the shared index. It must never block
# or fail session start — either being down just means code search is
# unavailable this session, not a broken start.
#
# Embeddings note: ollama (which used to serve `nomic-embed-text` for code
# search) is retired. Code search now reuses the bge-m3 embedder that
# `precis serve-embeddings` already runs on :8181, via a small
# OpenAI-compatible shim on :8182 (scripts/code-search/embed-shim.py). So this
# hook also starts that shim idempotently.
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

# Where the shell/Read/Edit actually operate this session — the current
# checkout's toplevel. In a `claude -w` worktree this differs from MAIN_ROOT;
# surfacing it (below) keeps the worktree path as available as the main one, so
# a `cd <main-repo>` reflex doesn't split the two checkouts (guard-cd-to-primary).
WORKTREE_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || WORKTREE_ROOT=""

# Bring Milvus up if it isn't. `up -d` returns fast when already running; on a
# cold machine it boots 3 containers (images are pre-pulled). Silence + never
# fail: no docker / no daemon → code search is simply off this session.
if command -v docker >/dev/null 2>&1; then
    docker compose -f "$COMPOSE" up -d >/dev/null 2>&1 || true
fi

# Start the OpenAI→bge-m3 embed shim if it isn't already listening. It's a
# host-wide singleton on :8182 (many worktrees, one shim) — the healthz probe
# makes this idempotent, so only the first session to come up starts it. Runs
# on the host (not a container): the bge-m3 embedder binds loopback, so a
# container couldn't reach it. Never block/fail: no python3, or bge-m3 (:8181)
# down, just means search_code is unavailable this session.
if command -v python3 >/dev/null 2>&1 &&
    ! curl -fsS -m 2 -o /dev/null http://127.0.0.1:8182/healthz 2>/dev/null; then
    nohup python3 scripts/code-search/embed-shim.py \
        >/tmp/code-search-embed-shim.log 2>&1 &
    disown 2>/dev/null || true
fi

if [[ -n "$MAIN_ROOT" ]]; then
    echo "🔎 code search (claude-context MCP): shared MAIN index — pass this ONLY as search_code's path= arg:"
    echo "   path=\"$MAIN_ROOT\" (hits are repo-relative → they map onto this worktree)."
    if [[ -n "$WORKTREE_ROOT" && "$WORKTREE_ROOT" != "$MAIN_ROOT" ]]; then
        echo "   ⚠ shell/Read/Edit operate in THIS worktree: $WORKTREE_ROOT"
        echo "     Run Bash bare (cwd is already here); never 'cd' to the MAIN path above — use 'git -C' to reach it."
    fi
    echo "🧭 exact who-calls / what-depends-on (Python): scripts/coderef callers|deps <file.py::Sym>"
    echo "   (structural, deterministic — prefer over grepping a bare symbol name)."
    if ! curl -fsS -m 2 -o /dev/null http://127.0.0.1:8181/healthz 2>/dev/null; then
        echo "   ⚠ code-search embedder down: bge-m3 (precis serve-embeddings :8181) unreachable → search_code will return nothing."
    fi
fi
exit 0

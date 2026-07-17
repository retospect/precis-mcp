#!/bin/bash
# dream-pass.sh — one dream cycle for asa.
#
# Thin wrapper around `precis worker --only dream_agent --once`.
# All the dispatch detail (model, max-turns, MCP config, disallowed
# tools, system prompt injection) lives in:
#
#   src/precis/workers/dream_agent.py (worker)
#   src/precis/utils/claude_agent.py  (unified claude -p helper)
#
# This script's only job is to:
#   * env-gate the run (PRECIS_DREAM_AGENT=1 turns it on; clearing
#     it pauses without uninstalling the LaunchDaemon)
#   * point the worker at the prompt + soul + MCP config files
#   * exec into the precis CLI
#
# The unified helper enforces cost cap, wall-clock timeout, and
# logs per-host attribution via the worker's BatchResult logger.
# See cluster/roles/precis_dream/README.md for the operator story.

set -e

soul=/Users/hermes/.asa/SOUL.md
mcp_config=/Users/hermes/.claude/mcp.json
# The dreaming workflow prompt now ships with precis-mcp
# (precis/data/prompts/dream-prompt.md); dream_agent loads it by default.
# Only the persona (SOUL) stays operator-side. Set
# PRECIS_DREAM_PROMPT_PATH below to override the packaged default.

if [ ! -f "$soul" ]; then
    echo "$(date -Iseconds) dream-pass: missing SOUL at $soul; skipping"
    exit 0
fi

export PRECIS_DREAM_AGENT=1
export PRECIS_DREAM_SOUL_PATH="$soul"
export PRECIS_MCP_CONFIG="$mcp_config"
export PRECIS_SOURCE=precis-dream

exec /opt/mcps/venv/bin/precis worker \
    --only dream_agent \
    --once \
    --batch-size 1

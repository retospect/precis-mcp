#!/usr/bin/env bash
# Run a precis-mcp usability pass with `claude -p`.
#
# Usage:
#   scripts/exercise-mcp/run.sh                       # broad pass with Opus
#   scripts/exercise-mcp/run.sh prompts/02-search.md  # different prompt
#   MODEL=claude-sonnet-4-6 scripts/exercise-mcp/run.sh prompts/02-search.md
#
# Env:
#   MODEL           default: claude-opus-4-7
#   MAX_BUDGET_USD  default: 5.00
set -euo pipefail

DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
OUT="$DIR/out"
mkdir -p "$OUT"

PROMPT_ARG="${1:-$DIR/prompts/01-broad.md}"
# Allow relative paths from repo root or from the harness dir.
if [[ -f "$PROMPT_ARG" ]]; then
  PROMPT="$PROMPT_ARG"
elif [[ -f "$DIR/$PROMPT_ARG" ]]; then
  PROMPT="$DIR/$PROMPT_ARG"
else
  echo "Prompt not found: $PROMPT_ARG" >&2
  exit 1
fi

MODEL="${MODEL:-claude-opus-4-7}"
MAX_BUDGET_USD="${MAX_BUDGET_USD:-5.00}"

# precis cold-start loads bge-m3 (~50s). Default MCP connect timeout is 30s.
export MCP_TIMEOUT="${MCP_TIMEOUT:-180000}"
export MCP_CONNECT_TIMEOUT_MS="${MCP_CONNECT_TIMEOUT_MS:-180000}"
export MCP_TOOL_TIMEOUT="${MCP_TOOL_TIMEOUT:-120000}"

STAMP="$(date +%Y-%m-%d-%H%M%S)"
NAME="$(basename "$PROMPT" .md)"
LOG="$OUT/${STAMP}-${NAME}.md"
DEBUG="$OUT/${STAMP}-${NAME}.debug.log"
META="$OUT/${STAMP}-${NAME}.meta.json"

# Sanity: docker image present, network exists.
if ! docker image inspect precis-mcp:latest >/dev/null 2>&1; then
  echo "precis-mcp:latest image missing. Build with:" >&2
  echo "  docker build --target runtime -t precis-mcp:latest -f docker/Dockerfile ." >&2
  exit 2
fi
if ! docker network inspect precis-infra_default >/dev/null 2>&1; then
  echo "Docker network precis-infra_default missing. Start postgres + watch with: pg, pdev, etc." >&2
  exit 3
fi

echo "Prompt:  $PROMPT" >&2
echo "Model:   $MODEL" >&2
echo "Out:     $LOG" >&2
echo "Debug:   $DEBUG" >&2
echo >&2

# Pre-flight the meta so we can correlate after.
cat > "$META" <<EOF
{
  "stamp": "$STAMP",
  "prompt": "$PROMPT",
  "model": "$MODEL",
  "max_budget_usd": "$MAX_BUDGET_USD",
  "mcp_config": "$DIR/mcp.json"
}
EOF

claude -p "$(cat "$PROMPT")" \
  --model "$MODEL" \
  --mcp-config "$DIR/mcp.json" \
  --strict-mcp-config \
  --permission-mode bypassPermissions \
  --no-session-persistence \
  --max-budget-usd "$MAX_BUDGET_USD" \
  --debug-file "$DEBUG" \
  > "$LOG"

echo >&2
echo "Done. Findings: $LOG" >&2
echo "Debug log:     $DEBUG" >&2

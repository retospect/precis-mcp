#!/usr/bin/env bash
# Run a precis-mcp paper-review pass with `claude -p`.
#
# Usage:
#   scripts/review-paper/run.sh <handle>                              # all personas (serial)
#   scripts/review-paper/run.sh <handle> <persona-slug>               # one pass only
#   MODEL=claude-sonnet-4-6 scripts/review-paper/run.sh <handle>      # different model
#
# Examples:
#   scripts/review-paper/run.sh paper:smith2024whatever
#   scripts/review-paper/run.sh paper:smith2024 precis-adversarial-reviewer
#
# Env:
#   MODEL           default: claude-opus-4-7
#   MAX_BUDGET_USD  default: 5.00 per pass
#
# Output:
#   scripts/review-paper/out/<stamp>-<handle>-<persona>.md
#   scripts/review-paper/out/<stamp>-<handle>-<persona>.debug.log
set -euo pipefail

DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$DIR/../.." && pwd)"
OUT="$DIR/out"
mkdir -p "$OUT"

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <handle> [persona-slug]" >&2
  echo >&2
  echo "  <handle> is the paper ref to review, e.g. 'paper:smith2024'." >&2
  echo "  Available personas:" >&2
  for f in "$REPO_ROOT"/src/precis/data/skills/personas/precis-*-reviewer.md; do
    echo "    $(basename "$f" .md)" >&2
  done
  exit 1
fi

HANDLE="$1"
SAFE_HANDLE="${HANDLE//[^a-zA-Z0-9._-]/_}"
PERSONA="${2:-ALL}"

MODEL="${MODEL:-claude-opus-4-7}"
MAX_BUDGET_USD="${MAX_BUDGET_USD:-5.00}"

# precis cold-start loads bge-m3 (~50s). Default MCP connect timeout is 30s.
export MCP_TIMEOUT="${MCP_TIMEOUT:-180000}"
export MCP_CONNECT_TIMEOUT_MS="${MCP_CONNECT_TIMEOUT_MS:-180000}"
export MCP_TOOL_TIMEOUT="${MCP_TOOL_TIMEOUT:-120000}"

# Sanity: docker image present, network exists.
if ! docker image inspect precis-mcp:latest >/dev/null 2>&1; then
  echo "precis-mcp:latest image missing. Build with:" >&2
  echo "  docker build --target runtime -t precis-mcp:latest -f docker/Dockerfile ." >&2
  exit 2
fi
if ! docker network inspect precis-infra_default >/dev/null 2>&1; then
  echo "Docker network precis-infra_default missing." >&2
  exit 3
fi

run_one() {
  local persona="$1"
  local stamp name log debug meta rendered
  stamp="$(date +%Y-%m-%d-%H%M%S)"
  name="${stamp}-${SAFE_HANDLE}-${persona}"
  log="$OUT/${name}.md"
  debug="$OUT/${name}.debug.log"
  meta="$OUT/${name}.meta.json"
  rendered="$OUT/${name}.prompt.md"

  # Render the persona: expand {{include}} directives + substitute <handle>.
  uv run --no-sync python "$DIR/_render_persona.py" \
      --persona "$persona" \
      --handle "$HANDLE" \
      > "$rendered"

  echo "Persona: $persona" >&2
  echo "Handle:  $HANDLE" >&2
  echo "Model:   $MODEL" >&2
  echo "Out:     $log" >&2
  echo "Debug:   $debug" >&2
  echo "Prompt:  $rendered" >&2
  echo >&2

  cat > "$meta" <<EOF
{
  "stamp": "$stamp",
  "handle": "$HANDLE",
  "persona": "$persona",
  "model": "$MODEL",
  "max_budget_usd": "$MAX_BUDGET_USD",
  "mcp_config": "$DIR/mcp.json"
}
EOF

  claude -p "$(cat "$rendered")" \
    --model "$MODEL" \
    --mcp-config "$DIR/mcp.json" \
    --strict-mcp-config \
    --permission-mode bypassPermissions \
    --no-session-persistence \
    --max-budget-usd "$MAX_BUDGET_USD" \
    --debug-file "$debug" \
    > "$log"

  echo "Done: $log" >&2
  echo >&2
}

if [[ "$PERSONA" == "ALL" ]]; then
  for f in "$REPO_ROOT"/src/precis/data/skills/personas/precis-*-reviewer.md; do
    run_one "$(basename "$f" .md)"
  done
else
  run_one "$PERSONA"
fi

echo "All passes complete. Reports under: $OUT" >&2

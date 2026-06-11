#!/usr/bin/env bash
# Run a precis-mcp usability pass with `claude -p`.
#
# Usage:
#   scripts/exercise-mcp/run.sh                       # broad pass with Opus
#   scripts/exercise-mcp/run.sh prompts/02-picky.md   # different prompt
#   MODEL=claude-sonnet-4-6 scripts/exercise-mcp/run.sh prompts/02-picky.md
#
# Env:
#   MODEL                  default: claude-opus-4-7
#   MAX_BUDGET_USD         default: 5.00
#   PRECIS_IMAGE           default: precis-mcp:dev
#   PRECIS_DOCKER_NETWORK  default: precis-infra_default
#   PRECIS_SECRETS_DIR     default: $HOME/.secrets/pw
#   PRECIS_CORPUS_DIR      default: $HOME/work/corpus
#   PRECIS_REPO_ROOT       default: derived from $DIR via git
#
# Each run writes four files under out/:
#   <stamp>-<name>.md          claude's report (stdout)
#   <stamp>-<name>.debug.log   claude --debug-file output
#   <stamp>-<name>.meta.json   run metadata
#   <stamp>-<name>.mcp.json    rendered MCP config used for this run
set -euo pipefail

DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
OUT="$DIR/out"
mkdir -p "$OUT"

# Repo-relative paths checked first so the harness's own prompts/ always win
# over stale files of the same name in cwd.
PROMPT_ARG="${1:-prompts/01-broad.md}"
if [[ -f "$DIR/$PROMPT_ARG" ]]; then
  PROMPT="$DIR/$PROMPT_ARG"
elif [[ -f "$PROMPT_ARG" ]]; then
  PROMPT="$PROMPT_ARG"
else
  echo "Prompt not found: $PROMPT_ARG (looked in $DIR and $PWD)" >&2
  exit 1
fi

MODEL="${MODEL:-claude-opus-4-7}"
MAX_BUDGET_USD="${MAX_BUDGET_USD:-5.00}"

PRECIS_IMAGE="${PRECIS_IMAGE:-precis-mcp:dev}"
PRECIS_DOCKER_NETWORK="${PRECIS_DOCKER_NETWORK:-precis-infra_default}"
PRECIS_SECRETS_DIR="${PRECIS_SECRETS_DIR:-$HOME/.secrets/pw}"
PRECIS_CORPUS_DIR="${PRECIS_CORPUS_DIR:-$HOME/work/corpus}"
PRECIS_REPO_ROOT="${PRECIS_REPO_ROOT:-$(git -C "$DIR" rev-parse --show-toplevel 2>/dev/null || (cd "$DIR/../.." && pwd))}"

# precis cold-start loads bge-m3 (~50s). Default MCP connect timeout is 30s.
export MCP_TIMEOUT="${MCP_TIMEOUT:-180000}"
export MCP_CONNECT_TIMEOUT_MS="${MCP_CONNECT_TIMEOUT_MS:-180000}"
export MCP_TOOL_TIMEOUT="${MCP_TOOL_TIMEOUT:-120000}"

# Seconds + pid avoids collisions on parallel invocations.
STAMP="$(date +%Y-%m-%d-%H%M%S)-$$"
NAME="$(basename "$PROMPT" .md)"
LOG="$OUT/${STAMP}-${NAME}.md"
DEBUG="$OUT/${STAMP}-${NAME}.debug.log"
META="$OUT/${STAMP}-${NAME}.meta.json"
MCP_CONFIG="$OUT/${STAMP}-${NAME}.mcp.json"

trap 'rc=$?; if [[ $rc -ne 0 ]]; then echo "" >&2; echo "claude exited $rc — see $DEBUG" >&2; fi' EXIT

# Preflight: required commands.
for cmd in docker jq claude; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Required command not found: $cmd" >&2
    exit 2
  fi
done

# Preflight: docker image + network.
if ! docker image inspect "$PRECIS_IMAGE" >/dev/null 2>&1; then
  echo "Docker image $PRECIS_IMAGE missing. Build with:" >&2
  echo "  docker build --target runtime -t $PRECIS_IMAGE -f docker/Dockerfile ." >&2
  echo "(Set PRECIS_IMAGE to use a different tag.)" >&2
  exit 2
fi
if ! docker network inspect "$PRECIS_DOCKER_NETWORK" >/dev/null 2>&1; then
  echo "Docker network $PRECIS_DOCKER_NETWORK missing. Start postgres + watch with: pg, pdev, etc." >&2
  echo "(Set PRECIS_DOCKER_NETWORK to override.)" >&2
  exit 3
fi

# Render the MCP config per run. Built with jq so paths containing quotes,
# backslashes, etc. can't break the JSON.
jq -n \
  --arg image "$PRECIS_IMAGE" \
  --arg network "$PRECIS_DOCKER_NETWORK" \
  --arg secrets "$PRECIS_SECRETS_DIR" \
  --arg corpus "$PRECIS_CORPUS_DIR" \
  --arg repo "$PRECIS_REPO_ROOT" \
  '{
    mcpServers: {
      precis: {
        command: "docker",
        args: [
          "run", "--rm", "-i",
          "--network", $network,
          "--add-host", "host.docker.internal:host-gateway",
          "-v", ($secrets + ":/secrets:ro"),
          "-v", ($corpus + ":/data/corpus:rw"),
          "-v", ($repo + ":/app:ro"),
          "-e", "LOG_LEVEL=WARNING",
          "-e", "PRECIS_EMBEDDER=bge-m3",
          "-e", "PRECIS_ORACLE_AUTO_REINGEST=0",
          "--entrypoint", "/usr/local/bin/docker-entrypoint.sh",
          $image,
          "precis", "serve"
        ]
      }
    }
  }' > "$MCP_CONFIG"

echo "Prompt:  $PROMPT" >&2
echo "Model:   $MODEL" >&2
echo "Out:     $LOG" >&2
echo "Debug:   $DEBUG" >&2
echo "MCP cfg: $MCP_CONFIG" >&2
echo >&2

# Meta JSON for downstream correlation. jq handles escaping; --argjson keeps
# max_budget_usd as a number and fails fast if it isn't numeric.
jq -n \
  --arg stamp "$STAMP" \
  --arg prompt "$PROMPT" \
  --arg model "$MODEL" \
  --arg mcp_config "$MCP_CONFIG" \
  --argjson max_budget_usd "$MAX_BUDGET_USD" \
  '{
    stamp: $stamp,
    prompt: $prompt,
    model: $model,
    max_budget_usd: $max_budget_usd,
    mcp_config: $mcp_config
  }' > "$META"

claude -p "$(cat "$PROMPT")" \
  --model "$MODEL" \
  --mcp-config "$MCP_CONFIG" \
  --strict-mcp-config \
  --permission-mode bypassPermissions \
  --no-session-persistence \
  --max-budget-usd "$MAX_BUDGET_USD" \
  --debug-file "$DEBUG" \
  > "$LOG"

echo >&2
echo "Done. Findings: $LOG" >&2
echo "Debug log:     $DEBUG" >&2

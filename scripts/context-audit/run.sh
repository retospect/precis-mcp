#!/usr/bin/env bash
# scripts/context-audit/run.sh — unattended context-quality audit pass.
#
# Chains capture.py (deterministic sampling) into one `claude -p` session that
# walks PROCEDURE.md end-to-end: reads every artifact under out/, judges each
# against RUBRIC.md, dedup-checks + files gripes for real defects, and reports
# the final pass/thin/bad tally + classifier-gap list. Mirrors
# scripts/exercise-mcp/run.sh's shape (one prompt, one precis MCP, artifacts
# under out/) — see that script for the option/env conventions this borrows.
#
# Usage:
#   scripts/context-audit/run.sh                          # capture + judge
#   scripts/context-audit/run.sh --skip-capture           # judge only (reuse out/)
#   MODEL=claude-sonnet-5 scripts/context-audit/run.sh
#
# Env:
#   MODEL                  default: claude-sonnet-5 (this is a bounded,
#                          decided-rubric judging pass, not novel design —
#                          sonnet tier, not opus; see CLAUDE.md "Agent sizing")
#   MAX_BUDGET_USD         default: 3.00
#   PRECIS_IMAGE           default: precis-mcp:dev
#   PRECIS_DOCKER_NETWORK  default: precis-infra_default
#   PRECIS_SECRETS_DIR     default: $HOME/.secrets/pw
#   PRECIS_CORPUS_DIR      default: $HOME/work/corpus
#   PRECIS_DATABASE_URL    dsn capture.py samples from — read-only prod hop
#                          (127.0.0.1:6432 as agent_rw) or a dev-DB precis.
#                          NOTE: the precis MCP claude connects to for the
#                          judging pass (below) is a SEPARATE connection —
#                          it's the one that files gripes, so point *that*
#                          one at a dev-DB precis for a practice run; never
#                          rely on this script to keep the two straight for
#                          you (dogfood rule, see PROCEDURE.md).
#
# Each run writes three files under out/:
#   <stamp>-audit.md          claude's report (stdout) — tally + findings
#   <stamp>-audit.debug.log   claude --debug-file output
#   <stamp>-audit.meta.json   run metadata
set -euo pipefail

DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
OUT="$DIR/out"
mkdir -p "$OUT"

SKIP_CAPTURE=0
if [[ "${1:-}" == "--skip-capture" ]]; then
  SKIP_CAPTURE=1
fi

MODEL="${MODEL:-claude-sonnet-5}"
MAX_BUDGET_USD="${MAX_BUDGET_USD:-3.00}"

PRECIS_IMAGE="${PRECIS_IMAGE:-precis-mcp:dev}"
PRECIS_DOCKER_NETWORK="${PRECIS_DOCKER_NETWORK:-precis-infra_default}"
PRECIS_SECRETS_DIR="${PRECIS_SECRETS_DIR:-$HOME/.secrets/pw}"
PRECIS_CORPUS_DIR="${PRECIS_CORPUS_DIR:-$HOME/work/corpus}"
PRECIS_REPO_ROOT="${PRECIS_REPO_ROOT:-$(git -C "$DIR" rev-parse --show-toplevel 2>/dev/null || (cd "$DIR/../.." && pwd))}"

export MCP_TIMEOUT="${MCP_TIMEOUT:-180000}"
export MCP_CONNECT_TIMEOUT_MS="${MCP_CONNECT_TIMEOUT_MS:-180000}"
export MCP_TOOL_TIMEOUT="${MCP_TOOL_TIMEOUT:-120000}"

for cmd in docker jq claude uv; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Required command not found: $cmd" >&2
    exit 2
  fi
done

if [[ "$SKIP_CAPTURE" -eq 0 ]]; then
  echo "Step 0 — capture" >&2
  uv run --project "$PRECIS_REPO_ROOT" "$DIR/capture.py" --out "$OUT"
else
  echo "Step 0 — skipped (--skip-capture); reusing existing $OUT/manifest.json" >&2
fi

if [[ ! -f "$OUT/manifest.json" ]]; then
  echo "$OUT/manifest.json missing — run without --skip-capture first." >&2
  exit 3
fi

if ! docker image inspect "$PRECIS_IMAGE" >/dev/null 2>&1; then
  echo "Docker image $PRECIS_IMAGE missing. Build with:" >&2
  echo "  docker build --target runtime -t $PRECIS_IMAGE -f docker/Dockerfile ." >&2
  exit 2
fi
if ! docker network inspect "$PRECIS_DOCKER_NETWORK" >/dev/null 2>&1; then
  echo "Docker network $PRECIS_DOCKER_NETWORK missing. Start postgres + watch with: pg, pdev, etc." >&2
  exit 3
fi

STAMP="$(date +%Y-%m-%d-%H%M%S)-$$"
LOG="$OUT/${STAMP}-audit.md"
DEBUG="$OUT/${STAMP}-audit.debug.log"
META="$OUT/${STAMP}-audit.meta.json"
MCP_CONFIG="$OUT/${STAMP}-audit.mcp.json"

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

PROMPT="You are running the precis-mcp context-quality audit.

Follow scripts/context-audit/PROCEDURE.md exactly, Step 1 through the final
tally (Step 0 — capture — has already run; out/manifest.json under
scripts/context-audit/ lists this run's artifacts). Apply
scripts/context-audit/RUBRIC.md to every non-skipped artifact. Dedup-check
before filing any gripe (search(kind='gripe', ...)) and file only genuinely
new defects, tagged as PROCEDURE.md Step 1c specifies. Read
docs/design/context-quality-eval.md if you need the catalog's 'why' for a
row.

End your report with: the pass/thin/bad tally, the classifier/pre-worker gap
list (separately, as its own section), and any skipped-catalog-row notes."

echo "Prompt:  (inline, PROCEDURE.md-driven)" >&2
echo "Model:   $MODEL" >&2
echo "Out:     $LOG" >&2
echo "Debug:   $DEBUG" >&2
echo "MCP cfg: $MCP_CONFIG" >&2
echo >&2

jq -n \
  --arg stamp "$STAMP" \
  --arg model "$MODEL" \
  --arg mcp_config "$MCP_CONFIG" \
  --arg manifest "$OUT/manifest.json" \
  --argjson max_budget_usd "$MAX_BUDGET_USD" \
  '{
    stamp: $stamp,
    procedure: "scripts/context-audit/PROCEDURE.md",
    manifest: $manifest,
    model: $model,
    max_budget_usd: $max_budget_usd,
    mcp_config: $mcp_config
  }' > "$META"

claude -p "$PROMPT" \
  --model "$MODEL" \
  --mcp-config "$MCP_CONFIG" \
  --strict-mcp-config \
  --permission-mode bypassPermissions \
  --no-session-persistence \
  --max-budget-usd "$MAX_BUDGET_USD" \
  --debug-file "$DEBUG" \
  > "$LOG"

echo >&2
echo "Done. Report:   $LOG" >&2
echo "Debug log:      $DEBUG" >&2

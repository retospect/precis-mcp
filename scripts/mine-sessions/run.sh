#!/usr/bin/env bash
# scripts/mine-sessions/run.sh — one window of session mining, end to end.
#
# Chains the four stages that turn raw agent transcripts into a scoreboard and
# a directory of evidence cards:
#
#   extract.py  every corpus        → out/events.jsonl   (normalized, scrubbed)
#   stats.py    events              → out/scoreboard.{md,json}
#   detect.py   events              → out/candidates.json
#   cards.py    events + candidates → out/cards/**       (+ INDEX.md)
#
# Nothing here judges anything — that is the pass, and it needs a model
# reading the cards (docs/runbooks/surface-review.md, `/surface-review`).
# This script exists so no future pass re-derives the extraction in a
# scratchpad, which is exactly what every prior pass did.
#
# Usage:
#   scripts/mine-sessions/run.sh                       # last 7d, local corpus
#   scripts/mine-sessions/run.sh --since 5d            # explicit window
#   scripts/mine-sessions/run.sh --since 14d --prod    # + ledger/llmlog/jobs
#   scripts/mine-sessions/run.sh --since 2d --limit 20 # smoke run
#
# Stage-specific options are ROUTED to the stage that owns them, not
# broadcast — each stage's argparse rejects flags it doesn't declare, so a
# blanket passthrough makes `--random 12` (a cards.py flag) abort extract.py:
#   --limit N, --projects-glob DIR   -> extract.py
#   --min-n N, --only DETECTOR       -> detect.py
#   --top N, --random N, --seed N    -> cards.py
#
# --prod adds the three prod corpora. They hop the cluster read-only through
# scripts/prod-psql; without it the run is purely local and needs no network.
set -euo pipefail

DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Artefacts land OUTSIDE the repo — see outdir.py for why (the secret gate
# scans the working tree, not the index, and mined transcripts are the most
# address-laden content this project produces). Keep this in sync with
# outdir.default_out_dir(); MINE_OUT overrides both.
OUT="${MINE_OUT:-${XDG_CACHE_HOME:-$HOME/.cache}/precis-mine-sessions/$(basename "$(cd "$DIR/../.." && pwd)")}"

SINCE="7d"
PROD=0
EXTRACT_ARGS=()
DETECT_ARGS=()
CARDS_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --since) SINCE="$2"; shift 2 ;;
        --since=*) SINCE="${1#*=}"; shift ;;
        --prod) PROD=1; shift ;;
        --out) OUT="$2"; shift 2 ;;
        --limit|--projects-glob) EXTRACT_ARGS+=("$1" "$2"); shift 2 ;;
        --min-n|--only) DETECT_ARGS+=("$1" "$2"); shift 2 ;;
        --top|--random|--seed) CARDS_ARGS+=("$1" "$2"); shift 2 ;;
        *) echo "mine-sessions: unknown option '$1'" >&2; exit 2 ;;
    esac
done

mkdir -p "$OUT"

SOURCES=(--local)
if [[ $PROD -eq 1 ]]; then
    SOURCES+=(--ledger --llmlog --jobs)
fi

echo "mine-sessions: window=$SINCE prod=$PROD out=$OUT" >&2

uv run "$DIR/extract.py" --since "$SINCE" --out "$OUT/events.jsonl" \
    "${SOURCES[@]}" "${EXTRACT_ARGS[@]+"${EXTRACT_ARGS[@]}"}"

uv run "$DIR/stats.py" --events "$OUT/events.jsonl" \
    --out-md "$OUT/scoreboard.md" --out-json "$OUT/scoreboard.json"

uv run "$DIR/detect.py" --events "$OUT/events.jsonl" \
    --out "$OUT/candidates.json" "${DETECT_ARGS[@]+"${DETECT_ARGS[@]}"}"

uv run "$DIR/cards.py" --events "$OUT/events.jsonl" \
    --candidates "$OUT/candidates.json" --out "$OUT/cards" \
    "${CARDS_ARGS[@]+"${CARDS_ARGS[@]}"}"

echo >&2
echo "mine-sessions: done." >&2
echo "  scoreboard  $OUT/scoreboard.md" >&2
echo "  candidates  $OUT/candidates.json" >&2
echo "  cards       $OUT/cards/INDEX.md" >&2

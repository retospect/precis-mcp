#!/bin/bash
# docker-entrypoint.sh - Read secrets from files and exec precis-mcp
#
# Supports _FILE suffix convention:
#   PRECIS_DATABASE_URL_FILE=/secrets/PRECIS_DATABASE_URL
#   -> reads file, sets PRECIS_DATABASE_URL

set -e

# Read secrets from files if _FILE suffix is provided
for var in PRECIS_DATABASE_URL PERPLEXITY_API_KEY SEMANTIC_SCHOLAR_API_KEY \
           WOLFRAM_APP_ID EPO_OPS_CLIENT_KEY EPO_OPS_CLIENT_SECRET \
           PRECIS_ROOT PRECIS_EMBEDDER; do
    file_var="${var}_FILE"
    if [ -n "${!file_var}" ] && [ -r "${!file_var}" ]; then
        export "$var"="$(cat "${!file_var}")"
        echo "[entrypoint] Loaded $var from ${!file_var}" >&2
    fi
done

# Also check /secrets/ directory directly
if [ -d /secrets ]; then
    for file in /secrets/*; do
        if [ -r "$file" ]; then
            varname=$(basename "$file")
            # Skip PG_* passwords (for postgres users only)
            if [[ ! "$varname" =~ ^PG_ ]]; then
                export "$varname"="$(cat "$file")"
            fi
        fi
    done
fi

# Validate required secrets — only for `precis …` invocations. Agent runs
# (`claude -p …` in the precis-agent image, which shares this entrypoint) are
# deliberately DB-less: diagnose_gripe/fix_gripe forward no DSN at all
# (workers/executors/agent_container.py::container_env), so an unconditional
# check here killed every such run before claude even started (job 210205).
if [ "${1:-}" = "precis" ]; then
    if [ -z "$PRECIS_DATABASE_URL" ]; then
        echo "[entrypoint] ERROR: PRECIS_DATABASE_URL not set" >&2
        exit 1
    fi
    # Self-migrate on `pip install -U`: a docker deploy has no orchestrator
    # running `precis migrate`, so the startup schema gate would otherwise
    # refuse to boot the upgraded image. Scoped to `precis …` argv on
    # purpose — this entrypoint is shared with agent `claude -p` runs whose
    # spawned `precis serve` sidecars must NOT auto-migrate a cluster DB
    # (the fleet migrates explicitly at deploy time).
    export PRECIS_AUTO_MIGRATE="${PRECIS_AUTO_MIGRATE:-1}"
fi

exec "$@"

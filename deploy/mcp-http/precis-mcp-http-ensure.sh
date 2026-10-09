#!/bin/bash
# precis-mcp-http-ensure.sh — idempotently ensure ONE long-lived streamable-http
# precis MCP server is running on this machine behind an always-up proxy, and
# print its client config.
#
# Replaces precis-mcp-dev-stdio.sh. Under stdio the MCP client owned the server
# process, so a `scripts/ship` that reset the bind-mounted checkout wedged the
# session with no way back (the install watchdog's os._exit recovery is
# disabled for source checkouts). Under streamable-http the client holds only
# an Mcp-Session-Id: a restarted server 404s that id, the client re-runs
# `initialize`, and the session carries on with a refreshed tool list.
# Spec + rationale: docs/backlog/session-mcp-http-server.md in the precis repo;
# this layout: deploy/mcp-http/README.md.
#
# Called from Claude Code's SessionStart hook. The FIRST session on the machine
# creates the containers; every later session sees them healthy and no-ops.
#
#   precis-mcp-http-ensure.sh                   # ensure up; quiet unless it acts
#   precis-mcp-http-ensure.sh --config          # print the ~/.claude.json entry
#   precis-mcp-http-ensure.sh --recreate        # force a blue-green backend swap
#   precis-mcp-http-ensure.sh --restart         # respawn the server child in place
#   precis-mcp-http-ensure.sh --status          # report without changing anything
#   precis-mcp-http-ensure.sh --migrate         # one-time cutover from the single-container layout
#   precis-mcp-http-ensure.sh --recreate-proxy  # replace the proxy container (a port gap: name a window)
#
# LAYOUT (organizer-mcp-1). A Caddy container ${NAME}-proxy owns 127.0.0.1:${PORT}
# and is never recreated by an ordinary run. It reverse-proxies over a private
# docker network (${NAME}-net) to the live backend, ${NAME}-a or ${NAME}-b, which
# publish no host port. The live colour is in ${STATE_DIR}/backend, mirrored by
# the upstream line of ${STATE_DIR}/caddy/Caddyfile (the Caddyfile wins if they
# disagree: it is what the proxy actually routes to). The name ${NAME} itself
# is no container any more, only the family prefix: `docker logs ${NAME}` would
# have shown Caddy's log, not the server's, to anyone with the old habit, so
# it now fails loudly instead; --status prints the live backend's name.
#
# A backend recreate is blue-green: start the other colour, wait until an
# unauthenticated POST /mcp to it answers 401 (the server is serving), validate
# and swap the Caddyfile, `caddy reload` (graceful), then `docker stop -t` the
# old colour so it drains. The port never closes. If any step before the swap
# fails the old backend stays live and this script exits non-zero.
#
# The backends are NOT --rm and ARE --restart unless-stopped: that restart
# policy is what replaces launchd's KeepAlive for the checkout watchdog, whose
# whole design is "exit cleanly and let something restart me".
#
# Recreates strand long-lived interactive sessions (they get 404 for their
# session id and re-initialise); child respawns do not (gr460711). So this
# script recreates a backend only when the container spec itself changed, and
# everything else — a secret rotation, `--restart` — respawns the child:
#   - The recreate label hashes the container spec (docker run args, image id,
#     the token the supervisor's liveness probe reads once), not this file:
#     an edit that leaves the spec alone does not recreate.
#   - Secrets are hashed separately. A change rewrites the mounted files and
#     SIGHUPs the serve child, which drains and exits; the next child reads
#     them at start.

set -euo pipefail

# Host-side state: mounted secrets copy, md vector cache, Caddyfile, the live
# colour, the log. Overridable so a test rig (another NAME/PORT) does not share
# prod's files.
STATE_DIR="${PRECIS_MCP_HTTP_STATE:-${HOME}/.cache/precis-mcp-http}"
# The main checkout: the image build context, mounted at /main (python/md
# kinds, worktree discovery — the agents' git worktrees live under it), and
# the parent of the prod clone below. Per-host: PRECIS_MCP_REPO, else the
# single path in ${STATE_DIR}/main-checkout, else the default layout. One
# path for all three, so a host never builds from one clone while its
# agents work in another.
REPO="${PRECIS_MCP_REPO:-}"
if [ -z "$REPO" ] && [ -f "${STATE_DIR}/main-checkout" ]; then
    REPO="$(tr -d '[:space:]' < "${STATE_DIR}/main-checkout")"
    if [ ! -d "${REPO}/.git" ]; then
        printf "precis-mcp: main-checkout: '%s' is not a git checkout; using the default\n" "$REPO" >&2
        REPO=""
    fi
fi
REPO="${REPO:-${HOME}/work/projects/code/precis-mcp}"
# What the server SERVES: a plain clone kept on origin/prod (the sha the
# cluster runs) — by scripts/deploy on the deploying machine, and by
# follow_prod below everywhere. REPO stays the build context for the image.
# No clone -> serve the main checkout, as before.
SRC_REPO="${PRECIS_MCP_SRC:-${REPO}-prod}"
if [ ! -f "${SRC_REPO}/.git/HEAD" ]; then
    SRC_REPO="$REPO"
fi
SECRETS="${HOME}/.secrets/pw"
IMAGE="${PRECIS_MCP_IMAGE:-precis-mcp:dev}"
NAME="${PRECIS_MCP_HTTP_NAME:-precis-mcp-http}"
PORT="${PRECIS_MCP_HTTP_PORT:-8765}"
# How long `docker stop` lets the serve child drain before SIGKILL. A slow
# cross-kind search measured 24 s on prod (gr457887); the child's own drain
# bound is 120 s, but this one blocks a SessionStart hook.
STOP_TIMEOUT="${PRECIS_MCP_HTTP_STOP_TIMEOUT:-45}"
# How long a new backend gets to answer its first 401. The supervisor binds the
# port at once but the child only serves after the prepare step (source copy,
# maybe a dependency sync), so this is the slow path's budget.
READY_TIMEOUT="${PRECIS_MCP_HTTP_READY_TIMEOUT:-300}"

# The proxy, pinned by tag AND digest so both dev machines run the same bytes
# and a moved upstream tag cannot be the next incident. v2.8.4, multi-arch
# index digest, pulled 2026-10-03. A new proxy image is a proxy recreate:
# `--recreate-proxy`, in a named window.
CADDY_IMAGE_PINNED='caddy:2.8.4-alpine@sha256:af32e97399febea808609119bb21544d0265c58a02836576e32a2d082c262c17'
CADDY_IMAGE="${PRECIS_MCP_CADDY_IMAGE:-$CADDY_IMAGE_PINNED}"

NET="${NAME}-net"
NET_FALLBACK_SUBNET=10.213.77.0/24   # private range; used only when docker's default pools are exhausted
PROXY="${NAME}-proxy"
BACKEND_PORT=8765   # inside the network; only the proxy publishes ${PORT}
CADDY_DIR="${STATE_DIR}/caddy"
CADDYFILE="${CADDY_DIR}/Caddyfile"
CADDY_MOUNT=/etc/precis-caddy   # the DIRECTORY is mounted, so `mv` replaces are seen
LOG="${STATE_DIR}/ensure.log"
STATE_BACKEND="${STATE_DIR}/backend"

MODE=ensure
case "${1:-}" in
    --config)         MODE=config ;;
    --recreate)       MODE=recreate ;;
    --restart)        MODE=restart ;;
    --status)         MODE=status ;;
    --migrate)        MODE=migrate ;;
    --recreate-proxy) MODE=recreate_proxy ;;
    "")               ;;
    *)  echo "usage: $(basename "$0") [--config|--recreate|--restart|--status|--migrate|--recreate-proxy]" >&2
        exit 2 ;;
esac

# The live server takes no test overrides (gr462596). On 2026-10-02 a test
# harness ran this script with a test port; the env hash changed, so it
# removed the live container and recreated it on that port, and every session
# gave up on the MCP. Overrides are for rigs, which use another NAME by
# construction; PRECIS_MCP_LIVE=1 is the deliberate exception.
if [ "$NAME" = precis-mcp-http ] && [ "${PRECIS_MCP_LIVE:-0}" != 1 ] && [ "$MODE" != status ]; then
    for var in PRECIS_MCP_HTTP_PORT PRECIS_MCP_HTTP_STATE PRECIS_MCP_IMAGE PRECIS_MCP_SRC \
               PRECIS_MCP_RUN_CMD PRECIS_MCP_ENTRYPOINT PRECIS_MCP_CADDY_IMAGE \
               PRECIS_MCP_CADDY_EXTRA PRECIS_MCP_DB_IP; do
        if [ -n "${!var:-}" ]; then
            echo "precis-mcp: refusing to touch the live ${NAME} with ${var} set;" \
                "a rig needs its own PRECIS_MCP_HTTP_NAME (PRECIS_MCP_LIVE=1 overrides)" >&2
            exit 3
        fi
    done
fi

TOKEN_FILE="${SECRETS}/PRECIS_MCP_TOKEN"

mint_token() {
    [ -s "$TOKEN_FILE" ] && return 0
    mkdir -p "$SECRETS"
    ( umask 077; openssl rand -hex 32 > "$TOKEN_FILE" )
    chmod 600 "$TOKEN_FILE"
}

client_config() {
    printf '{"type":"http","url":"http://127.0.0.1:%s/mcp","headers":{"Authorization":"Bearer %s"}}\n' \
        "$PORT" "$(cat "$TOKEN_FILE")"
}

if [ "$MODE" = config ]; then
    mint_token
    client_config
    exit 0
fi

# --- helpers ----------------------------------------------------------------
# log: one line to ${LOG} (so a client's "session expired" can be matched to
# its recreate). note: the same, and to stderr for the operator.
now() { date -u +%FT%TZ; }
log() { mkdir -p "$STATE_DIR"; printf '%s\n' "$*" >> "$LOG"; }
note() { log "$*"; printf 'precis-mcp: %s\n' "$*" >&2; }

bname() { echo "${NAME}-$1"; }
other_colour() { if [ "$1" = a ]; then echo b; else echo a; fi; }
exists() { docker inspect "$1" >/dev/null 2>&1; }
running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null || echo false)" = true ]; }
label_of() { docker inspect -f "{{index .Config.Labels \"$2\"}}" "$1" 2>/dev/null || true; }

# The colour the proxy actually routes to: the Caddyfile's upstream line, else
# the state file, else a.
live_colour() {
    local c=""
    if [ -f "$CADDYFILE" ]; then
        c="$(grep -o "${NAME}-[ab]:${BACKEND_PORT}" "$CADDYFILE" 2>/dev/null | head -1 \
            | sed "s/^${NAME}-//; s/:.*//" || true)"
    fi
    if [ -z "$c" ] && [ -s "$STATE_BACKEND" ]; then
        c="$(head -c1 "$STATE_BACKEND")"
    fi
    case "$c" in a|b) echo "$c" ;; *) echo a ;; esac
}

# The single-container layout this replaces: a container named ${NAME} that
# publishes the host port itself.
is_legacy() {
    exists "$NAME" && [ -n "$(docker inspect -f '{{if .HostConfig.PortBindings}}yes{{end}}' "$NAME" 2>/dev/null)" ]
}

# HTTP status of an unauthenticated POST /mcp to a backend over the private
# network, from inside the proxy container (or a throwaway one on the same
# network when the proxy is not up yet). Prints the status or nothing. 401 is
# the server's own answer: it proves the backend is serving HTTP. The Host
# header is what clients send, so the server's Host validation sees no change.
probe_backend() {
    local out
    local -a cmd=(wget -S -q -T 3 -O /dev/null
        --header "Host: 127.0.0.1:${PORT}"
        --header 'Content-Type: application/json'
        --header 'Accept: application/json, text/event-stream'
        --post-data '{}' "http://$1:${BACKEND_PORT}/mcp")
    if running "$PROXY"; then
        out="$(docker exec "$PROXY" "${cmd[@]}" 2>&1 || true)"
    else
        out="$(docker run --rm --network "$NET" "$CADDY_IMAGE" "${cmd[@]}" 2>&1 || true)"
    fi
    printf '%s\n' "$out" | sed -n 's|^ *HTTP/1\.[01] \([0-9][0-9][0-9]\).*|\1|p' | head -1
}

http_code() {  # host-side status of a request, 000 when nothing answered
    curl -s -m "$1" -o /dev/null -w '%{http_code}' "${@:2}" 2>/dev/null || true
}

status_report() {
    local live lname health direct via
    echo "layout:   $(if is_legacy; then echo "LEGACY single container ${NAME} (run --migrate)"; else echo "proxy + blue-green backends"; fi)"
    echo "proxy:    $(docker ps -a --filter "name=^/${PROXY}$" --format '{{.Names}}  {{.Status}}')"
    health="$(http_code 3 "http://127.0.0.1:${PORT}/proxy-health")"
    echo "proxy:    GET  127.0.0.1:${PORT}/proxy-health -> ${health}  (200 = Caddy is up and listening)"
    live="$(live_colour)"
    lname="$(bname "$live")"
    echo "backend:  live colour ${live} = ${lname}  ($(docker ps -a --filter "name=^/${lname}$" --format '{{.Status}}  env={{.Label "precis.env_hash"}}'))"
    if running "$PROXY"; then
        direct="$(probe_backend "$lname")"
        echo "backend:  POST ${lname}:${BACKEND_PORT}/mcp (no token, from the proxy) -> ${direct:-no answer}  (401 = serving)"
    fi
    # The proxy retries dials for 60 s, so a down backend shows as a hang here.
    via="$(http_code 5 -X POST -H 'Content-Type: application/json' --data '{}' "http://127.0.0.1:${PORT}/mcp")"
    echo "backend:  POST 127.0.0.1:${PORT}/mcp (no token, via proxy) -> ${via}  (401 = serving, 502 = backend down, 000 = no answer in 5 s)"
}

if [ "$MODE" = status ]; then
    status_report
    exit 0
fi

# --- serialise concurrent SessionStart hooks -------------------------------
# Twelve sessions can start within the same second; without a lock they race to
# `docker run` the same name and eleven fail noisily. mkdir is the atomic
# primitive available everywhere (macOS ships no flock(1)).
LOCK="${TMPDIR:-/tmp}/${NAME}-ensure.lock"
got_lock=false
for _ in $(seq 1 200); do
    if mkdir "$LOCK" 2>/dev/null; then
        trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT
        got_lock=true
        break
    fi
    # Break a lock orphaned by a killed hook (older than 2 minutes).
    if [ -d "$LOCK" ] && [ -z "$(find "$LOCK" -maxdepth 0 -mmin -2 2>/dev/null)" ]; then
        rmdir "$LOCK" 2>/dev/null || true
    fi
    sleep 0.3
done
if ! $got_lock; then
    # Another hook is mid-recreate (a drain can take STOP_TIMEOUT). Proceeding
    # unlocked would race it to recreate the same container; it will finish.
    echo "precis-mcp: another ensure holds ${LOCK}; leaving it to that run" >&2
    exit 0
fi

# Keep the served clone on origin/prod. scripts/deploy moves it only on the
# machine that runs the deploy, so a second machine hosting this server
# served a stale sha while deploys ran elsewhere (06e3f3d7 under a 0dc5e6a0
# prod, 2026-10-02). Following the ref works wherever the deploy ran;
# the server's checkout watchdog then drains and restarts onto it. At most one
# fetch a minute: twelve sessions starting together must not fetch twelve times.
follow_prod() {
    [ "$SRC_REPO" != "$REPO" ] || return 0
    local stamp="${STATE_DIR}/.prod-fetched" want have
    mkdir -p "$STATE_DIR"
    if [ -n "$(find "$stamp" -mmin -1 2>/dev/null)" ]; then
        return 0
    fi
    touch "$stamp"
    git -C "$SRC_REPO" fetch -q origin prod 2>/dev/null || return 0
    want="$(git -C "$SRC_REPO" rev-parse FETCH_HEAD)"
    have="$(git -C "$SRC_REPO" rev-parse HEAD)"
    [ "$want" != "$have" ] || return 0
    if git -C "$SRC_REPO" checkout -q --detach "$want" 2>/dev/null; then
        echo "precis-mcp: prod clone ${have:0:8} → ${want:0:8} (origin/prod)" >&2
    else
        echo "precis-mcp: prod clone NOT moved to ${want:0:8} — local edits in ${SRC_REPO}?" >&2
    fi
}
follow_prod

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    cat >&2 <<EOF
precis-mcp: image '$IMAGE' not found. Build it (also needed after any
dependency change in pyproject.toml — the image carries the venv, /app
carries only the source):

  cd $REPO && docker build --target dev \\
    --build-arg UID=\$(id -u) --build-arg GID=\$(id -g) \\
    --build-context premodels=docker-image://precis-mcp:premodels \\
    -t precis-mcp:dev -f docker/Dockerfile .
EOF
    exit 2
fi
if ! docker image inspect "$CADDY_IMAGE" >/dev/null 2>&1; then
    echo "precis-mcp: proxy image '${CADDY_IMAGE}' not present; pulling the pinned image" >&2
    docker pull "$CADDY_IMAGE" >&2
fi

mint_token
TOKEN="$(cat "$TOKEN_FILE")"

# The database node is cluster topology and this file is public: its name comes
# from the environment or ${SECRETS}/PRECIS_MCP_DB_HOST, never from the repo.
# The container reaches it by name, mapped to its Tailscale IP via --add-host.
DB_HOST="${PRECIS_MCP_DB_HOST:-}"
if [ -z "$DB_HOST" ] && [ -s "${SECRETS}/PRECIS_MCP_DB_HOST" ]; then
    DB_HOST="$(head -1 "${SECRETS}/PRECIS_MCP_DB_HOST")"
fi
if [ -z "$DB_HOST" ]; then
    echo "precis-mcp: database node not named. Put its Tailscale hostname in" \
        "${SECRETS}/PRECIS_MCP_DB_HOST (or PRECIS_MCP_DB_HOST)." >&2
    exit 3
fi
DB_IP="${PRECIS_MCP_DB_IP:-}"
if [ -z "$DB_IP" ]; then
    TS="$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)"
    DB_IP="$("$TS" ip -4 "$DB_HOST" 2>/dev/null | head -1 || true)"
fi
if [ -z "$DB_IP" ]; then
    echo "precis-mcp: cannot resolve ${DB_HOST}'s Tailscale IP — is Tailscale up?" >&2
    exit 3
fi

DB_URL="$(sed "s#@host.docker.internal:#@${DB_HOST}:#" "${SECRETS}/PRECIS_DATABASE_URL")"

# gr458350: secrets reach the container as files under /run/precis-secrets
# (not /secrets: docker-entrypoint.sh exports every file there as env), never as -e
# values (those print in docker-inspect output, ps and /proc/<pid>/environ).
# precis reads PRECIS_DATABASE_URL and PRECIS_MCP_TOKEN from
# PRECIS_SECRETS_FILE_DIR, and the API keys through get_secret's file layer.
# The DSN is rewritten for the container, so this is a copy, not a mount of
# ${SECRETS}. Built in a staging dir and synced into the mounted one only when
# it differs, so an unchanged run never touches the live files. Both colours
# mount the same directory.
SECRETS_OUT="${STATE_DIR}/secrets"
SECRETS_NEW="${STATE_DIR}/secrets.new"
# The md vector cache (~19k vectors) used to live in the container's writable
# layer, so every recreate threw it away and re-warmed for tens of minutes
# with the embedder saturated. Both colours mount it; during a swap the two
# overlap for the new one's start-up.
CACHE_DIR="${STATE_DIR}/cache"
mkdir -p "$CACHE_DIR" "$CADDY_DIR"
# uv's cache, so a fresh container's prepare step is not a cold download.
# A named volume, not a host bind: tens of thousands of cache files on the
# virtiofs share are the file-handle load that panicked a dev machine on
# 2026-10-02. Docker seeds an empty named volume from the image's copy.
UV_CACHE_VOLUME="${NAME}-uv-cache"
(
    umask 077
    rm -rf "$SECRETS_NEW"
    mkdir -p "$SECRETS_OUT" "$SECRETS_NEW"
    printf '%s\n' "$DB_URL" > "${SECRETS_NEW}/PRECIS_DATABASE_URL"
    printf '%s\n' "$TOKEN" > "${SECRETS_NEW}/PRECIS_MCP_TOKEN"
    for name in PERPLEXITY_API_KEY SEMANTIC_SCHOLAR_API_KEY WOLFRAM_APP_ID \
                EPO_OPS_CLIENT_KEY EPO_OPS_CLIENT_SECRET; do
        if [ -s "${SECRETS}/${name}" ]; then
            cp "${SECRETS}/${name}" "${SECRETS_NEW}/${name}"
        fi
    done
)
dir_hash() {
    # Names and contents, so an added or removed key counts as a change.
    (cd "$1" && for f in *; do if [ -f "$f" ]; then printf '%s\0' "$f"; cat "$f"; fi; done) \
        | shasum -a 256 | cut -c1-16
}
SECRETS_HASH="$(dir_hash "$SECRETS_NEW")"

sync_secrets() {
    # Rewrite the mounted directory in place (it is a directory bind mount, so
    # new files and renames show up inside the running container).
    local f
    for f in "$SECRETS_OUT"/*; do
        [ -e "$f" ] || continue
        [ -e "${SECRETS_NEW}/$(basename "$f")" ] || rm -f "$f"
    done
    for f in "$SECRETS_NEW"/*; do
        mv -f "$f" "${SECRETS_OUT}/$(basename "$f")"
    done
    rm -rf "$SECRETS_NEW"
}

# Sizing for ~12 concurrent sessions (spec §Sizing). One tool permit per
# session so no session's search queues behind another's; the DB pool must stay
# above that, with headroom for the background embedder/md-index warmups.
TOOL_CONCURRENCY="${PRECIS_MCP_TOOL_CONCURRENCY:-12}"
POOL_MIN="${PRECIS_DB_POOL_MIN_SIZE:-4}"
POOL_MAX="${PRECIS_DB_POOL_MAX_SIZE:-16}"

# Extra python-kind roots for this host: one `alias:/absolute/host/path` per
# line in ${STATE_DIR}/python-roots (# comments allowed). Per-host so the
# public repo names no machine's layout. Each is mounted read-only at
# /roots/<alias>; a change alters the container spec, so the next run
# recreates (blue-green) on its own.
# What /main mounts: REPO (resolved at the top, ${STATE_DIR}/main-checkout
# included); PRECIS_MCP_MAIN overrides it for a test rig.
MAIN_CHECKOUT="${PRECIS_MCP_MAIN:-$REPO}"
if [ ! -d "${MAIN_CHECKOUT}/.git" ]; then
    note "PRECIS_MCP_MAIN: '${MAIN_CHECKOUT}' is not a git checkout; using ${REPO}"
    MAIN_CHECKOUT="$REPO"
fi

PY_ROOTS="precis:/app,main:/main"
EXTRA_MOUNTS=()
if [ -f "${STATE_DIR}/python-roots" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%%#*}"
        line="$(printf '%s' "$line" | tr -d '[:space:]')"
        [ -n "$line" ] || continue
        root_name="${line%%:*}"
        root_path="${line#*:}"
        if ! [[ "$root_name" =~ ^[a-z][a-z0-9_-]*$ ]] || [ "$root_name" = precis ] \
            || [ "$root_name" = main ] || [ "${root_path:0:1}" != / ] || [ ! -d "$root_path" ]; then
            note "python-roots: skipping '${line}' (want name:/existing/dir, name not precis/main)"
            continue
        fi
        PY_ROOTS="${PY_ROOTS},${root_name}:/roots/${root_name}"
        EXTRA_MOUNTS+=(-v "${root_path}:/roots/${root_name}:ro")
    done < "${STATE_DIR}/python-roots"
fi

ENVS=(
    -e PRECIS_SECRETS_FILE_DIR=/run/precis-secrets
    -e PRECIS_ROOT=/data/notes
    -e PRECIS_PYTHON_ROOTS="${PY_ROOTS}"
    -e PRECIS_PYTHON_WORKTREES=wt:/main
    -e PRECIS_PYTHON_GITDIR_MAP="${MAIN_CHECKOUT}:/main"
    -e PRECIS_MD_ROOTS=repo:/app,main:/main
    -e PRECIS_EMBEDDER=remote
    -e PRECIS_EMBEDDER_URL=http://host.docker.internal:8181
    -e PRECIS_MCP_TOOL_CONCURRENCY="${TOOL_CONCURRENCY}"
    -e PRECIS_DB_POOL_MIN_SIZE="${POOL_MIN}"
    -e PRECIS_DB_POOL_MAX_SIZE="${POOL_MAX}"
    # Bounce the server when the source checkout's HEAD moves (ship/sync/qland).
    # Keyed on the resolved sha, NOT mtimes — a git checkout touches every
    # mtime, which is exactly why the install-watchdog arm refuses source trees.
    -e PRECIS_CHECKOUT_WATCHDOG=/src
)

# Snapshot the checkout instead of bind-mounting it live. With one long-lived
# process a live mount is actively harmful: a half-applied `git checkout` is a
# half-consistent import tree. /src is the read-only mount, /app the private
# copy the baked editable install already points at. Cost is amortised per
# SERVER lifetime now, not per session. Excludes .git (97 MB) and .claude
# (6.9 GB of sibling worktrees) — neither is importable source.
#
# The image carries the venv, /app only the source, so a deploy can outrun the
# image two ways, and the prepare step closes both, each behind a stamp in the
# venv so an unchanged tree costs two sha256s:
#   - A new or bumped dependency (numba, 2026-10-02: the child crash-looped on
#     ModuleNotFoundError until a manual image rebuild). When uv.lock changes,
#     dry-run the image's own locked sync; run it only if the plan adds a
#     package or changes a version (same-version reinstalls — one cu13 wheel
#     shows up in every plan — are not worth a download on the restart path).
#     --inexact removes nothing; autocatpath is skipped because it comes from
#     the private catpath repo, which needs a token the container lacks.
#   - New entry points: the venv is a path-editable install, so new modules
#     import from /app but entry points stay at the image build's. When
#     pyproject.toml changes, reinstall the project metadata (--no-deps, ~4 s).
# A failure is logged and the child starts anyway; a dependency failure says
# to rebuild the image (a dep with no wheel needs the builder's compiler).
#
# PID 1 is precis.mcp_supervisor (gr459481): it binds the port once and runs
# `precis serve --fd` as a child, re-running the prepare step before each one,
# so a watchdog restart replaces the child while connections wait in the listen
# backlog instead of being refused. It runs from its own directory (never
# from /app, which every child start wipes) and changes only on a container
# recreate. A checkout without the supervisor falls back to the old launch.
RUN_CMD='set -e
cat > /tmp/precis-prepare.sh <<"PREP"
set -e
t0=$SECONDS
rm -rf /app/* /app/.[!.]* 2>/dev/null || true
tar -C /src --exclude=./.git --exclude=./.claude --exclude=./.venv \
    --exclude=./node_modules -cf - . | tar -C /app -xf -
t1=$SECONDS
lstamp=/opt/venv/.precis-uvlock.sha256
lwant="$(sha256sum /app/uv.lock | cut -d" " -f1)"
if [ "$(cat "$lstamp" 2>/dev/null)" != "$lwant" ]; then
  sync=(uv sync --frozen --no-install-project --all-extras --no-dev --inexact
        --no-install-package autocatpath)
  # A failed dry-run must not read as "nothing new": that would stamp the
  # lock and skip the sync until uv.lock next changes.
  if ! plan="$(cd /app && "${sync[@]}" --dry-run 2>&1)"; then
    echo "precis-prepare: dependency dry-run FAILED: $(printf "%s" "$plan" | tail -1)" >&2
    new=""; lwant=""
  else
    adds="$(printf "%s\n" "$plan" | sed -n "s/^ + //p" | sort)"
    drops="$(printf "%s\n" "$plan" | sed -n "s/^ - //p" | sort)"
    new="$(comm -23 <(printf "%s\n" "$adds") <(printf "%s\n" "$drops") | sed "/^$/d")"
  fi
  if [ -z "$lwant" ]; then
    :
  elif [ -z "$new" ]; then
    printf "%s\n" "$lwant" > "$lstamp"
  elif (cd /app && "${sync[@]}" --quiet); then
    printf "%s\n" "$lwant" > "$lstamp"
    echo "precis-prepare: synced dependencies from uv.lock:" $new >&2
  else
    echo "precis-prepare: dependency sync FAILED (wanted:" $new ") — rebuild the image" >&2
  fi
fi
t2=$SECONDS
stamp=/opt/venv/.precis-pyproject.sha256
want="$(sha256sum /app/pyproject.toml | cut -d" " -f1)"
if [ "$(cat "$stamp" 2>/dev/null)" != "$want" ]; then
  if uv pip install --quiet --python /opt/venv/bin/python --no-deps -e /app; then
    printf "%s\n" "$want" > "$stamp"
    echo "precis-prepare: reinstalled project metadata (pyproject $want)" >&2
  else
    echo "precis-prepare: metadata reinstall FAILED; entry points may be stale" >&2
  fi
fi
# One line per child start: where a slow start spent its time.
echo "precis-prepare: copy $((t1-t0))s, deps $((t2-t1))s, metadata $((SECONDS-t2))s" >&2
PREP
if [ -f /src/src/precis/mcp_supervisor.py ] && [ -f /src/src/precis/mcp_liveness.py ]; then
  rm -rf /tmp/precis-supervisor && mkdir -p /tmp/precis-supervisor
  cp /src/src/precis/mcp_supervisor.py /src/src/precis/mcp_liveness.py /tmp/precis-supervisor/
  exec /opt/venv/bin/python /tmp/precis-supervisor/mcp_supervisor.py --host 0.0.0.0 --port '"${BACKEND_PORT}"' \
      --prepare "bash /tmp/precis-prepare.sh" \
      -- precis serve --transport streamable-http --fd "{fd}"
fi
bash /tmp/precis-prepare.sh
exec precis serve --transport streamable-http --host 0.0.0.0 --port '"${BACKEND_PORT}"

# Rig hooks (refused on the live name above): a fake backend replaces the
# server so the proxy and the swap can be tested without the image or the DB.
RUN_CMD="${PRECIS_MCP_RUN_CMD:-$RUN_CMD}"
ENTRYPOINT="${PRECIS_MCP_ENTRYPOINT:-/usr/local/bin/docker-entrypoint.sh}"

# No -p: only the proxy publishes a host port. The backends are reachable by
# container name on ${NET}.
RUN_ARGS=(
    --restart unless-stopped
    --network "$NET"
    --add-host "${DB_HOST}:${DB_IP}"
    --add-host host.docker.internal:host-gateway
    -v "${HOME}/work/corpus:/data/corpus:ro"
    -v "${HOME}/work:/data/notes:ro"
    -v "${SRC_REPO}:/src:ro"
    # Live main checkout (read-only) so a qland is visible via the python/md
    # kinds before a deploy; roots main:/main in ENVS above.
    -v "${MAIN_CHECKOUT}:/main:ro"
    -v "${SECRETS_OUT}:/run/precis-secrets:ro"
    -v "${CACHE_DIR}:/home/precis/.cache/precis"
    -v "${UV_CACHE_VOLUME}:/home/precis/.cache/uv"
    ${EXTRA_MOUNTS[@]+"${EXTRA_MOUNTS[@]}"}
    "${ENVS[@]}"
    --entrypoint "$ENTRYPOINT"
)

# A named, non-ephemeral container pins its spec at CREATION: `docker restart`
# will not pick up a changed sizing knob. Stamp a hash of everything that goes
# into creation and recreate on mismatch. The token is in it because the
# supervisor's liveness probe reads it once, at container start.
ENV_HASH="$(
    { printf '%s\0' "${RUN_ARGS[@]}" "$IMAGE" "$RUN_CMD" "$TOKEN"
      docker image inspect -f '{{.Id}}' "$IMAGE"
    } | shasum -a 256 | cut -c1-16
)"

# The proxy's creation spec; a mismatch is reported, never acted on (see
# --recreate-proxy).
PROXY_SPEC="$(printf '%s\0' "$CADDY_IMAGE" "$PORT" "$NET" "$BACKEND_PORT" "$CADDY_MOUNT" | shasum -a 256 | cut -c1-16)"

# --- the proxy ----------------------------------------------------------------

# Rig hook PRECIS_MCP_CADDY_EXTRA: extra lines inside the site block.
caddyfile_for() {
    cat <<EOF
{
	auto_https off
	admin localhost:2019
}

:${BACKEND_PORT} {
	handle /proxy-health {
		respond "ok" 200
	}
	handle {
		# lb_try_duration retries DIAL failures only (refused, name not yet
		# resolvable) for 60 s, so a request arriving while a backend is coming
		# up waits instead of failing; a request a backend accepted is never
		# replayed. request_buffers: a retry re-sends the request body, and
		# Caddy 2.8 cannot unless it buffered it (without it the retry that
		# finally dials fails 502 "invalid Read on closed Body", measured).
		# MCP calls are small; a body over the limit streams and cannot be
		# retried. flush_interval -1 passes the SSE stream unbuffered. Host,
		# Authorization and Mcp-Session-Id reach the backend unchanged.
		reverse_proxy ${1}:${BACKEND_PORT} {
			lb_try_duration 60s
			lb_try_interval 500ms
			request_buffers 8MB
			flush_interval -1
			transport http {
				dial_timeout 2s
			}
		}
	}
${PRECIS_MCP_CADDY_EXTRA:-}
}
EOF
}

# caddy validate on a file in ${CADDY_DIR}: inside the running proxy, else in a
# throwaway container with the same mount.
caddy_validate() {
    local f="$1"
    if running "$PROXY"; then
        docker exec "$PROXY" caddy validate --config "${CADDY_MOUNT}/${f}" --adapter caddyfile 2>&1
    else
        docker run --rm -v "${CADDY_DIR}:${CADDY_MOUNT}:ro" "$CADDY_IMAGE" \
            caddy validate --config "${CADDY_MOUNT}/${f}" --adapter caddyfile 2>&1
    fi
}

# Point the proxy at ${1} (a container name), keeping the old config live on any
# failure. The new file is validated BEFORE it replaces anything; the swap is an
# atomic `mv` in the same directory (the directory, not the file, is mounted, so
# the proxy sees the replacement); `caddy reload` is graceful; if it fails the
# old file is restored (a failed reload leaves the old config running).
switch_proxy() {
    local upstream="$1" from="$2" to="$3" tmp out
    tmp="Caddyfile.new.$$"
    caddyfile_for "$upstream" > "${CADDY_DIR}/${tmp}"
    if ! out="$(caddy_validate "$tmp")"; then
        rm -f "${CADDY_DIR:?}/${tmp:?}"
        printf '%s\n' "$out" >&2
        note "proxy: caddy validate REJECTED the new config; NOT switching, the old config and backend ${from} stay live"
        return 1
    fi
    if [ -f "$CADDYFILE" ]; then
        cp -p "$CADDYFILE" "${CADDYFILE}.prev"
    fi
    mv -f "${CADDY_DIR}/${tmp}" "$CADDYFILE"
    if ! out="$(docker exec "$PROXY" caddy reload --config "${CADDY_MOUNT}/Caddyfile" --adapter caddyfile 2>&1)"; then
        printf '%s\n' "$out" >&2
        if [ -f "${CADDYFILE}.prev" ]; then
            mv -f "${CADDYFILE}.prev" "$CADDYFILE"
        fi
        note "proxy: caddy reload FAILED; restored the old Caddyfile, the old config and backend ${from} stay live"
        return 1
    fi
    printf '%s\n' "$to" > "$STATE_BACKEND"
    log "proxy: ${from}→${to} $(now)"
    echo "precis-mcp: proxy switched ${from}→${to}" >&2
}

create_proxy() {  # $1 = container name; created, not started
    docker create \
        --name "$1" \
        --label "precis.proxy_spec=${PROXY_SPEC}" \
        --restart unless-stopped \
        --network "$NET" \
        -p "127.0.0.1:${PORT}:${BACKEND_PORT}" \
        -v "${CADDY_DIR}:${CADDY_MOUNT}:ro" \
        --tmpfs /data --tmpfs /config \
        "$CADDY_IMAGE" \
        caddy run --config "${CADDY_MOUNT}/Caddyfile" --adapter caddyfile >/dev/null
}

start_proxy() {  # start $PROXY and wait for Caddy's own health answer
    local i
    # Colima's port forwarder can take a moment to release the port after the
    # previous holder stopped, and a start that races it fails: retry.
    for i in $(seq 1 15); do
        if docker start "$PROXY" >/dev/null 2>&1; then break; fi
        if [ "$i" -ge 15 ]; then
            echo "precis-mcp: proxy ${PROXY} will not start:" >&2
            docker start "$PROXY" >&2 || true
            return 1
        fi
        sleep 1
    done
    for _ in $(seq 1 60); do
        [ "$(http_code 2 "http://127.0.0.1:${PORT}/proxy-health")" = 200 ] && return 0
        sleep 0.5
    done
    echo "precis-mcp: proxy ${PROXY} started but /proxy-health does not answer on 127.0.0.1:${PORT}:" >&2
    docker logs --tail 30 "$PROXY" >&2 || true
    return 1
}

ensure_network() {
    local out
    docker network inspect "$NET" >/dev/null 2>&1 && return 0
    if [ -n "${PRECIS_MCP_NET_SUBNET:-}" ]; then
        docker network create --subnet "$PRECIS_MCP_NET_SUBNET" "$NET" >/dev/null
        return
    fi
    if out="$(docker network create "$NET" 2>&1)"; then
        return 0
    fi
    # Every compose test stack on a busy dev machine holds a network, and the
    # daemon's default address pools hold about thirty ("all predefined address
    # pools have been fully subnetted", seen 2026-10-03 with 30 present). An
    # explicit subnet does not draw on those pools.
    case "$out" in
        *"fully subnetted"*)
            echo "precis-mcp: docker has no free default network pool; using ${NET_FALLBACK_SUBNET}" \
                "(override with PRECIS_MCP_NET_SUBNET)" >&2
            docker network create --subnet "$NET_FALLBACK_SUBNET" "$NET" >/dev/null ;;
        *)  printf '%s\n' "$out" >&2; return 1 ;;
    esac
}

# Create the proxy if missing; start it if stopped; otherwise leave it alone.
# A spec difference (new pinned image, new port) is reported, never applied.
ensure_proxy() {
    ensure_network
    if [ ! -f "$CADDYFILE" ]; then
        caddyfile_for "$(bname "$(live_colour)")" > "${CADDYFILE}.init"
        mv -f "${CADDYFILE}.init" "$CADDYFILE"
    fi
    if ! exists "$PROXY"; then
        create_proxy "$PROXY"
        start_proxy
        note "proxy: created ${PROXY} on 127.0.0.1:${PORT} (${CADDY_IMAGE})"
    elif ! running "$PROXY"; then
        start_proxy
        note "proxy: ${PROXY} was stopped; started it"
    elif [ "$(label_of "$PROXY" precis.proxy_spec)" != "$PROXY_SPEC" ]; then
        echo "precis-mcp: ${PROXY} was created with a different spec than this script pins;" \
            "leaving it running. Replace it in a named window with --recreate-proxy." >&2
    fi
}

# The one remaining port gap: stop-to-bind of the proxy container. Named
# window only. Everything but the container itself is state on disk.
do_recreate_proxy() {
    trap '' HUP INT TERM
    ensure_network
    local next="${PROXY}-next"
    docker rm -f "$next" >/dev/null 2>&1 || true
    create_proxy "$next"
    if exists "$PROXY"; then
        docker stop -t 10 "$PROXY" >/dev/null 2>&1 || true
        docker rm -f "$PROXY" >/dev/null 2>&1 || true
    fi
    docker rename "$next" "$PROXY"
    start_proxy
    note "proxy: recreated ${PROXY} (${CADDY_IMAGE}) $(now)"
}

# --- the backends -------------------------------------------------------------

create_backend() {  # $1 = colour; created and started, not yet verified
    local name
    name="$(bname "$1")"
    docker rm -f "$name" >/dev/null 2>&1 || true
    # The image has no ~/.cache/uv, so docker creates the volume root-owned and
    # uv cannot write it. Before anything switches: not in a dark window.
    if [ -z "${PRECIS_MCP_RUN_CMD:-}" ]; then
        docker run --rm --user 0 --entrypoint chown -v "${UV_CACHE_VOLUME}:/c" "$IMAGE" precis: /c
    fi
    docker create \
        --name "$name" \
        --label "precis.env_hash=${ENV_HASH}" \
        --label "precis.colour=$1" \
        -e "PRECIS_MCP_COLOUR=$1" \
        "${RUN_ARGS[@]}" \
        "$IMAGE" \
        bash -c "$RUN_CMD" >/dev/null
    docker start "$name" >/dev/null
    log "backend: started ${name} env=${ENV_HASH} $(now)"
}

# Wait (bounded) for backend ${1} to answer 401. On failure print its logs.
wait_backend_ready() {
    local name="$1" deadline code
    deadline=$(( $(date +%s) + READY_TIMEOUT ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if ! running "$name"; then
            echo "precis-mcp: ${name} exited before it answered:" >&2
            docker logs --tail 40 "$name" >&2 || true
            return 1
        fi
        code="$(probe_backend "$name")"
        [ "$code" = 401 ] && return 0
        sleep 1
    done
    echo "precis-mcp: ${name} did not answer 401 within ${READY_TIMEOUT}s; its log:" >&2
    docker logs --tail 40 "$name" >&2 || true
    return 1
}

drain_backend() {  # stop (drain) then remove backend ${1}
    local name="$1"
    exists "$name" || return 0
    log "drain: stopping ${name} (stop -t ${STOP_TIMEOUT}) $(now)"
    # SIGTERM reaches the supervisor, which forwards it to the serve child
    # as SIGHUP (drain, then exit); `-t` bounds the drain.
    docker stop -t "$STOP_TIMEOUT" "$name" >/dev/null 2>&1 || true
    docker rm -f "$name" >/dev/null 2>&1 || true
    log "drain: ${name} stopped and removed $(now)"
}

respawn_child() {
    # SIGHUP the supervisor's serve child: it drains its in-flight calls and
    # exits 0, and the supervisor starts the next generation on the port it
    # still holds. (A child predating the drain handler just dies of SIGHUP,
    # which the supervisor also treats as a restart.)
    local name pid
    name="$(bname "$(live_colour)")"
    pid="$(docker exec "$name" bash -c '
        for d in /proc/[0-9]*; do
            read -r _ _ _ ppid _ < "$d/stat" 2>/dev/null || continue
            [ "$ppid" = 1 ] || continue
            grep -qaz -x serve "$d/cmdline" 2>/dev/null && echo "${d#/proc/}"
        done' 2>/dev/null | head -1)"
    if [ -z "$pid" ]; then
        echo "precis-mcp: no serve child found in ${name} (mid-respawn?); not signalled" >&2
        return 1
    fi
    docker exec "$name" bash -c "kill -HUP $pid"
    echo "precis-mcp: respawning ${name}'s server child (pid ${pid})" >&2
}

# Blue-green: start the other colour, prove it serves, swap the proxy, drain the
# old. Any failure before the swap leaves the old backend live and exits 1.
recreate_backend() {
    local live next lname nname
    live="$(live_colour)"; next="$(other_colour "$live")"
    lname="$(bname "$live")"; nname="$(bname "$next")"
    sync_secrets
    # A hook killed mid-swap must not leave a half-done state: ignore the
    # signals a hook timeout or a closed terminal sends (SIG_IGN survives exec,
    # so the docker CLI calls ignore them too).
    trap '' HUP INT TERM
    create_backend "$next"
    if ! wait_backend_ready "$nname"; then
        docker rm -f "$nname" >/dev/null 2>&1 || true
        note "backend: ${nname} never became ready; NOT switching, ${lname} stays live"
        exit 1
    fi
    if ! switch_proxy "$nname" "$live" "$next"; then
        docker logs --tail 20 "$nname" >&2 || true
        docker rm -f "$nname" >/dev/null 2>&1 || true
        exit 1
    fi
    drain_backend "$lname"
    echo "precis-mcp: started ${nname} behind 127.0.0.1:${PORT} (env ${ENV_HASH})" >&2
}

# One-time cutover from the single-container layout (a container named ${NAME}
# publishing the port). The one gap in this design: the old container holds the
# port until it has drained, so the dark window is drain-end to proxy start —
# the same kind as the old recreate, once. The proxy is created before the old
# container stops so its start is as short as it can be.
do_migrate() {
    trap '' HUP INT TERM
    note "migrate: ${NAME} (single container) -> ${PROXY} + ${NAME}-a $(now)"
    ensure_network
    printf 'a\n' > "$STATE_BACKEND"
    caddyfile_for "$(bname a)" > "${CADDYFILE}.init"
    mv -f "${CADDYFILE}.init" "$CADDYFILE"
    sync_secrets
    docker rm -f "${NAME}-next" >/dev/null 2>&1 || true
    create_backend a
    if ! wait_backend_ready "$(bname a)"; then
        docker rm -f "$(bname a)" >/dev/null 2>&1 || true
        note "migrate: ${NAME}-a never became ready; the old ${NAME} is untouched and still serving"
        exit 1
    fi
    docker rm -f "$PROXY" >/dev/null 2>&1 || true
    create_proxy "$PROXY"
    log "migrate: stopping legacy ${NAME} $(now)"
    docker stop -t "$STOP_TIMEOUT" "$NAME" >/dev/null 2>&1 || true
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    start_proxy
    log "proxy: legacy→a $(now)"
    echo "precis-mcp: migrated; ${PROXY} on 127.0.0.1:${PORT}, ${NAME}-a live (env ${ENV_HASH})" >&2
}

# --- main ---------------------------------------------------------------------

if is_legacy; then
    if [ "$MODE" = migrate ] || [ "$MODE" = recreate ]; then
        do_migrate
        exit 0
    fi
    echo "precis-mcp: ${NAME} is still the single-container layout; leaving it serving." \
        "Run '$(basename "$0") --migrate' in a named window to install the proxy." >&2
    exit 0
fi
if [ "$MODE" = migrate ]; then
    echo "precis-mcp: nothing to migrate (no single-container ${NAME})" >&2
    MODE=ensure
fi

ensure_proxy
if [ "$MODE" = recreate_proxy ]; then
    do_recreate_proxy
    exit 0
fi

LIVE="$(live_colour)"
LNAME="$(bname "$LIVE")"
if [ "$MODE" != recreate ] && running "$LNAME" && [ "$(label_of "$LNAME" precis.env_hash)" = "$ENV_HASH" ]; then
    # The secrets label is stamped at creation and not updatable, so the
    # mounted copy is the live record of what the server was given.
    if [ "$(dir_hash "$SECRETS_OUT")" != "$SECRETS_HASH" ]; then
        sync_secrets
        respawn_child || true
    elif [ "$MODE" = restart ]; then
        rm -rf "$SECRETS_NEW"
        respawn_child
    else
        rm -rf "$SECRETS_NEW"
    fi
    # A previous swap killed after the reload may have left the old colour up.
    drain_backend "$(bname "$(other_colour "$LIVE")")"
    exit 0
fi

if ! exists "$LNAME"; then
    # Nothing is serving yet (first install): no old backend to protect.
    sync_secrets
    trap '' HUP INT TERM
    create_backend "$LIVE"
    wait_backend_ready "$LNAME" || exit 1
    printf '%s\n' "$LIVE" > "$STATE_BACKEND"
    log "proxy: none→${LIVE} $(now)"
    echo "precis-mcp: started ${LNAME} behind 127.0.0.1:${PORT} (env ${ENV_HASH})" >&2
    exit 0
fi

recreate_backend

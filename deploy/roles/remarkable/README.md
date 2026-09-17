# remarkable role — build the send-to-tablet uploader image

Builds the `precis-remarkable` container image (docker/remarkable) on the
**agent host** so the `remarkable_send` job can push a compiled draft PDF to the
reMarkable cloud in a throwaway container. Sibling of `roles/tts` +
`roles/aizynth`, but macOS/agent-host instead of Linux/compute-node, and
**image-only** — the env + credential are already provisioned elsewhere.

## What this role does (and doesn't)

**Does:** assert the container runtime, copy `docker/remarkable` to the node,
build `precis-remarkable:<tag>` (the image fetches the pinned `ddvk/rmapi`
release for the node arch — `arm64` under Apple-silicon colima).

**Doesn't wire env** — because it's already there:
- `PRECIS_CONTAINER_BIN` is set by `roles/precis_worker_agent` (the colima
  docker CLI the container executor already uses).
- `PRECIS_REMARKABLE_IMAGE` rides the existing `precis_shared_env` dict, which
  the worker-agent plist already renders in a loop — no template edit needed.
- The device credential is **per user**: `REMARKABLE_RMAPI_CONFIG:<login>`
  in the secrets vault (ADR 0055), paired self-service on `/account`. The
  driver resolves the sending user's entry at run time and passes it into the
  container **by key** (env `REMARKABLE_RMAPI_CONFIG`), never on argv. It is
  never a plist var and there is no deployment-wide fallback.

## Run (on demand — NOT in redeploy-precis.yml)

    ansible-playbook playbooks/47-remarkable.yml
    # skip the image build once present:
    ansible-playbook playbooks/47-remarkable.yml -e remarkable_build_image=false

Gated by topology: the role runs where `inventory_hostname in
precis_capabilities.remarkable_send`. Add the agent host (the gateway) to that
list in `inventory/group_vars/all/topology.yml`.

## Arming the send (S0 ops, one-time, Reto-gated)

1. **Pair a device** — each user pairs their own tablet on `/account`
   (one-time code from <https://my.remarkable.com/device/apps/connect>, or
   paste an `rmapi.conf` body in the advanced box). No ops step: the
   credential lands in `vault.secrets` as `REMARKABLE_RMAPI_CONFIG:<login>`.
   The ansible vault holds only deploy-time infra secrets (DB / TLS /
   tailscale) — app credentials live in `vault.secrets` alone.
2. (no separate vaulting step — pairing is the vaulting)
3. **Build the image** — `ansible-playbook playbooks/47-remarkable.yml`.
4. **Point the worker at it** — add
   `PRECIS_REMARKABLE_IMAGE: precis-remarkable:<sha>` to `precis_shared_env`
   and re-run `playbooks/37-precis-worker-agent.yml` (re-renders the plist +
   restarts the worker).

Until a user has paired, the feature is **dark for that user**: the web
button is hidden (gated on `remarkable_configured`) and the job reports "no
device paired". Until
step 4, the send falls back to on-PATH `rmapi` (absent on the host → a clean
"not installed" report), so nothing half-works.

## Rollback

Clear `PRECIS_REMARKABLE_IMAGE` from `precis_shared_env` (send falls back to
the in-process path); a user unpairs on `/account` (button hides, job
declines for that user). The image can be removed with
`docker rmi {{ remarkable_image }}`.

# reMarkable send — device pairing (ops, Reto)

The feature is shipped dark; the button stays hidden until pairing: rmapi
8-char code → vault REMARKABLE_RMAPI_CONFIG → playbooks/47-remarkable.yml →
set PRECIS_REMARKABLE_IMAGE in precis_shared_env + re-run the agent-worker
role. Three unverified externals at first run (check, don't blind-trust):
exact ddvk/rmapi release asset names, the rmapi.conf format, colima
bind-mount sharing on macOS. Docs: docker/remarkable/README.md.

Per-user self-service pairing now exists on `/account` — any signed-in
user can pair their own tablet with a one-time code
(`precis.export.remarkable.register_device`) with no ops involvement, and
that credential wins over the deployment-wide one. This item stays open
for the global/deployment device + the container-image rollout above,
which individual pairing doesn't replace.

**rmapi version constraint** (verified 2026-08-31): v0.0.34 (currently
pinned in deploy/roles/remarkable/docker/remarkable/Dockerfile) fails with
HTTP 400 from the reMarkable cloud on `mkdir` and `put` (auth and `ls` work).
v0.0.35+ fixes all verbs. The container-image and binary rollout to worker
hosts must pin v0.0.35 or later. Related prod failure: remarkable_papers_send
and remarkable_reading_send jobs on melchior failed with "rmapi binary not
installed" (jobs 273957/273958/274777, 2026-08-30/31); when the worker-host
binary rollout lands, it must use v0.0.35+.

**Token-exchange rate limit — send_pdf discards the cached user token**
(observed 2026-08-31, local 94-paper push with a real credential):
`send_pdf` writes the vault credential to a fresh throwaway tempfile per
call, so every invocation makes rmapi redo the device-token → user-token
exchange; the reMarkable auth endpoint 429s after roughly a dozen rapid
exchanges, which will break any multi-document job
(`remarkable_papers_send` / `remarkable_reading_send`) the moment the
binary rollout lands. Fix direction: persist the refreshed rmapi config
(user token) across sends within one job — a per-job config file handed
to every rmapi invocation, or write the refreshed body back to the vault —
plus modest pacing/backoff on 429 in the send loop.

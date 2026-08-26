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

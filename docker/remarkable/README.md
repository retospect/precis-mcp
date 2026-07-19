# precis-remarkable — the send-to-tablet uploader image

A per-tool sidecar container (sibling of `docker/tts`, `docker/aizynth`) whose
**only** job is to run the [`ddvk/rmapi`](https://github.com/ddvk/rmapi) Go
binary — the maintained fork that speaks the moving-target reMarkable cloud
sync protocol and uploads a PDF non-interactively (`rmapi put`).

## Why a container

The reMarkable send is the one part of the draft-export path that adds a
**foreign binary** (a Go release, not a pip dep), **cloud network egress**, and
a **live device credential**. Containerising just that keeps all three off the
worker host: the worker still compiles the RM2 PDF with its own MacTeX (already
provisioned for `draft_export`) and drives this image with a one-shot
`docker run` per send. The image is **code + binary only** — no models, no
precis wheel — so it stays tiny and the sync-protocol churn is isolated to the
version-pinned `rmapi` binary.

## Contract (bind mount + one by-key secret)

```
in : /work/in/doc.pdf         the compiled PDF to upload
     /work/in/params.json     {"folder","name","timeout_s"}
env: REMARKABLE_RMAPI_CONFIG  the rmapi config body (devicetoken: …), passed
                              --env by KEY (value inherited from the worker
                              process, never in argv / ref_events)
out: /work/out/result.json    {"ok","returncode","output","name","folder"}
```

The credential is **never baked into the image** and **never on the command
line** — it rides the container env by key, exactly the `agent_container.py`
secret convention. The device pairing (below) mints the config once; it lives
in the secrets vault (ADR 0055) and flows into the worker process, which
forwards it by key.

## Build (on the worker node)

    docker build -t precis-remarkable:<sha> \
      --build-arg RMAPI_VERSION=v0.0.34 docker/remarkable

`RMAPI_VERSION` pins the `ddvk/rmapi` release; the build detects the arch
(`aarch64` → the Apple-silicon colima default on melchior, `x86_64` for a Linux
node) and fetches the matching release binary. Tag the image with a sha and
point the worker at it via `PRECIS_REMARKABLE_IMAGE`.

## Run (one-shot per send — the driver composes this)

    docker run --rm \
      -v <scratch>/in:/work/in:ro -v <scratch>/out:/work/out \
      --env REMARKABLE_RMAPI_CONFIG \
      precis-remarkable:<sha>
    # reads /work/in/doc.pdf -> uploads -> writes /work/out/result.json

`precis.export.remarkable.send_via_container` stages the scratch, builds this
argv (via `build_container_argv`), passes the credential by key, runs it, and
parses `result.json` into a `SendResult`. `send_pdf` dispatches here whenever
`PRECIS_REMARKABLE_IMAGE` is set; otherwise it falls back to an on-PATH `rmapi`
(dev + tests, via the `PRECIS_RMAPI_BIN` stub).

## One-time device registration (S0 ops, Reto-gated)

`rmapi` needs a device token, minted once from an interactive pairing:

1. On any machine, run `rmapi` and paste the 8-letter code from
   <https://my.remarkable.com/device/desktop/connect>.
2. Take the resulting config body (`~/.config/rmapi/rmapi.conf`, at minimum
   `devicetoken: <token>`) and vault it as `vault_remarkable_rmapi_config` →
   the worker env `REMARKABLE_RMAPI_CONFIG` (ADR 0055).
3. A deploy builds this image on the worker node (colima on melchior) and sets
   `PRECIS_REMARKABLE_IMAGE` + `PRECIS_CONTAINER_BIN` in the worker-agent env.
   This rides the same Phase-2 container-executor ops window as the rest of the
   factory container cutover.

## Scratch sharing on macOS

Under colima/Docker Desktop the bind-mounted scratch must sit inside a shared
path. Set `PRECIS_REMARKABLE_SCRATCH` to a shared root (e.g. under `$HOME`) if
the default system temp isn't mounted into the VM.

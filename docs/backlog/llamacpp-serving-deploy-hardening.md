# llama.cpp serving deploy — two fragilities (retry + pin)

Both surfaced 2026-08-12 unifying SMALL on glm-4.7-flash. Neither broke live
serving, but both break `playbooks/04-llamacpp.yml`. Owner:
`deploy/roles/llamacpp/`.

## 1. GGUF download has no retry vs caspar's CDN-bridge blackhole

`tasks/download.yml` already sets `HF_HUB_DISABLE_XET=1` (caspar can't reach
`cas-server.xethub.hf.co`), so downloads take the classic path — which 302s to
the CDN bridge `us.aws.cdn.hf.co`. That host is **per-IP blackholed** from
caspar: some AWS IPs 200-OK, others hang to a ~10s TLS-handshake timeout (DNS
returns a rotating pool). The task runs `hf download` **once** (async, no
retry), so a single bad-IP hit fails the whole deploy with
`httpx.ConnectTimeout`. Manual workaround this session: a ~40-iteration retry
loop (hf resumes partials) landed it on the second-to-last try.

Fix: add `retries`/`delay`/`until: <result> is succeeded` to both download
tasks (single-file + multi-part). With `poll > 0` ansible waits on the async
job, so `until` composes. ~20 retries × 15s covers the blackhole. Belt-and-
suspenders: also try pinning a good IP via `--header Host` or resolving, but
retry alone is enough.

## 2. Build tracks a moving `master` → pulls broken HEAD

`defaults/main.yml` sets `llamacpp_git_ref: "master"`, so every deploy pulls
llama.cpp's latest HEAD and rebuilds. On 2026-08-12 that HEAD (ggml 0.19.0 /
4dd1275) had a `vendor/cpp-httplib` cmake regression — `OpenSSL::SSL` target
not found → `cmake --build` rc=2 → the `build` tag fails on every inference
node. Serving survived (the pre-existing binary is untouched; build precedes
the service bounce, and `scripts/deploy`/`redeploy-precis.yml` does NOT run
04-llamacpp). Workaround: `--tags config,service` skips the build; the existing
binary serves current models fine (glm-4.7-flash loads as arch=`deepseek2`).

Fix: pin `llamacpp_git_ref` to a known-good llama.cpp release tag (a `bNNNN`
release, not `master`) so builds are reproducible and a broken upstream HEAD
can't wedge a serving deploy. Validate the chosen tag builds on **both** the
Linux/CUDA GPU node and macOS/Metal before pinning (the OpenSSL issue is
macOS-cmake-specific; don't pin blind). This is an external dep — pinning a
release is correct, not the internal-SHA anti-pattern.

Effort: (1) mechanical; (2) needs a cross-platform build check before the pin.

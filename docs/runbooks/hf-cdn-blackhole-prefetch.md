# HF GGUF download times out on caspar — CDN-bridge blackhole pre-fetch

**Symptom.** `hf download <repo> <file>` on **caspar** (the `gguf_store_hosts`
data node for `04-llamacpp.yml`) dies after ~10s with
`httpx.ConnectTimeout: _ssl.c:1112: The handshake operation timed out` — for
some GGUF files but not others. Ansible shows `failed=1` at "Download
single-file GGUFs via hf CLI".

**Root cause (diagnosed 2026-08-12).** Xet is already disabled by the role
(`HF_HUB_DISABLE_XET=1` in `deploy/roles/llamacpp/tasks/download.yml`), so
the unreachable `cas-server.xethub.hf.co` is not the problem. With xet off,
`hf` pulls from the CDN bridge `us.aws.cdn.hf.co`, which is **per-IP
blackholed** from caspar — DNS returns a rotating AWS pool; some IPs 200-OK,
others (IPv4 13.37.x / 98.90.x ranges) hang to timeout. The download task
runs `hf` **once** (async, no retry), so a single bad-IP hit fails the whole
deploy. `huggingface.co` itself is always reachable.

## Workaround — manual pre-fetch, then re-run the play

On caspar, as the `deploy` user, loop until `hf` lands on a good IP (it
resumes partials):

```sh
for i in $(seq 1 40); do
  HF_HUB_DISABLE_XET=1 /opt/llamacpp/venv/bin/hf download <repo> <file> \
    --local-dir /opt/nfs/shared/gguf/<name> && break
  sleep 5
done
```

Then re-run `playbooks/04-llamacpp.yml --limit caspar:melchior` — the
download task is idempotent (skips a present, size/etag-matching file) and
proceeds to sync → llamacpp_hosts → config → bounce. Verify size with BSD
stat: `stat -f %z <file>` (caspar is macOS).

## Durable fix (filed, not yet shipped)

Add `retries/delay/until` to the two download tasks in
`deploy/roles/llamacpp/tasks/download.yml` so a bad-IP hit retries onto a
reachable CDN IP. Tracked in
`docs/backlog/llamacpp-serving-deploy-hardening.md`, which also covers the
sibling gotcha found the same day: the role default
`llamacpp_git_ref: "master"` tracks llama.cpp HEAD, and a broken upstream
HEAD fails the `build` tag fleet-wide (serving survives — the existing
binary is untouched, and `scripts/deploy` does not run `04-llamacpp`).
Interim dodge for a broken HEAD: `--tags config,service` to skip the build.

# Llamacpp fleet ops

Grouped 2026-09-26 from 3 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## llama.cpp serving deploy — two fragilities (retry + pin)

_Grouped 2026-09-26; was `llamacpp-serving-deploy-hardening`._

Both surfaced 2026-08-12 unifying SMALL on glm-4.7-flash. Neither broke live
serving, but both break `playbooks/04-llamacpp.yml`. Owner:
`deploy/roles/llamacpp/`.

### 1. GGUF download has no retry vs caspar's CDN-bridge blackhole

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

### 2. Build tracks a moving `master` → pulls broken HEAD

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

## llamacpp GGUF fleet convergence has no automation; catalog has drifted

_Grouped 2026-09-26; was `llamacpp-model-convergence`._

The role's design is sound — caspar downloads canonically into NFS
(`deploy/roles/llamacpp/tasks/download.yml`, writes `manifest.yaml` with
per-model SHA256) → each inference node rsyncs its declared
`host_vars[*].llamacpp_models` subset to local SSD (`tasks/sync.yml`) →
serves local; the catalog is meant to be source of truth. Findings
(read-only, 2026-08-05): **no convergence automation** — sync is a manual
`ansible-playbook deploy/playbooks/04-llamacpp.yml --tags sync`, no
cron/launchd/timer anywhere; last converged 2026-04-24 (all local GGUFs
4.5 months stale). Catalog≠reality, and a blind sync is destructive: spark
declares `llamacpp_models: []` but is hand-edited live to serve the 80B (a
deploy re-render already wiped it once, breaking the quest loop); melchior
holds an undeclared DeepSeek-R1-70B orphan; balthazar matches. **No
runtime verification** — clients never check local files against
`manifest.yaml`'s SHA256, so silent partial-sync/bitrot is undetectable.

Fix path: (1) reconcile the catalog to intent (declare spark's real served
set so converging is non-destructive; resolve melchior's orphan); (2)
converge once (`--tags download` to refresh canonical+manifest, then
`sync,config,service`; verify against manifest SHA256); (3) automate
convergence — a scheduled per-node systemd timer / launchd running
catalog-driven rsync+prune+checksum-verify, nodes self-heal; (4) optional:
a `gguf_check` system-profile precis pass (sibling of `disk_check.py` from
gr191008) raising `kind='alert'` on drift — warn on stale/missing,
critical on served-model-missing or checksum mismatch.

Promoted from gr194396's sibling gr194304.

## spark pair: spend the ~76G idle headroom on a larger big-tier model

_Grouped 2026-09-26; was `spark-pair-larger-model`._

Measured 2026-08-10 with DeepSeek-V4-Flash UD-Q8_K_XL (151G, RPC-sharded)
serving at `-np 4` (4×~32k slots): castor 82G used / 38G available, pollux
83G / 38G — ~76G of the pair's 242G sits idle. A slightly larger model (or
bigger quant / more KV) would fit in a ~210–220G total budget: e.g. the
Qwen3-235B Q6_K (180G, already on castor's disk) sharded across the pair,
or a higher-precision DeepSeek quant if one lands in that window. Leave
headroom for KV growth (scales with slots × ctx) and the OS.

Opposite direction from the parked Q3 single-box experiment (Q3_K_XL ~104G
on castor alone, freeing pollux) — decide which way to spend the pair
before doing either. Model switch = relink `~/serve-current.sh` on castor +
`systemctl restart llama-server` (~10 min load, big tier falls to cloud
rung meanwhile); keep card `served_by[0].max_parallel` + `resource_slots`
in lockstep with `-np` (memory: spark-pair-big-model-serving).

test: new model serves on the pair, tok/s single + @4-way measured vs
DeepSeek's 10.3/27.2, `llm.chain.big` repointed, zero llm_call_log errors
over a day.

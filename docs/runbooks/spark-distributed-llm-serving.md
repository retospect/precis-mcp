# Distributed llama.cpp across castor + pollux

**What.** Big-model inference sharded across the two paired DGX Sparks via
**llama.cpp RPC over the 200G ConnectX/RoCE link**. Live since 2026-08-05Z.
Node bringup and the interconnect:
[`dgx-spark-node-bringup`](./dgx-spark-node-bringup.md).

**Endpoint (OpenAI-compatible).** castor, port 8080, bound to all interfaces;
web chat UI at the root, API under `/v1` (any dummy key). Reachable on the
cluster LAN (not over the tailnet unless a subnet route is advertised —
[`cluster-ssh-remote-access`](./cluster-ssh-remote-access.md)). Address in the
overlay. `/health` returns 503 `{"Loading model"}` until warm.

## Topology

- castor = **main node**: `llama-server`, holds ~half the layers on its GB10,
  serves the API.
- pollux = **RPC worker**: `ggml-rpc-server` (port 50052, bound to its CX-link
  address), holds the other half. castor loads the weights and pushes pollux's
  share over the CX link at load time — the worker needs **no** model file.
- RPC auto-negotiates true RDMA/RoCEv2. ~**10 tok/s** for Qwen3-235B (MoE, 22B
  active); the per-token RPC hop caps speed.

## Persistence (systemd, both `enabled`, survive reboot)

- pollux `llama-rpc.service` → `/home/deploy/start-rpc.sh` →
  `ggml-rpc-server -d CUDA0 -H <cx addr> -p 50052 -c`. The `-c` tensor disk
  cache (`~/.cache/llama.cpp/rpc/`) writes pollux's ~96 GB share to NVMe on
  the FIRST load (slow once) but speeds reloads after a reboot. The script
  waits for the CX IP before binding.
- castor `llama-server.service` → `ExecStartPre=/home/deploy/wait-rpc.sh`
  (waits for pollux's RPC port) → `/home/deploy/serve-current.sh`.

## Switch models = repoint a symlink + restart

`serve-current.sh` is a symlink to `serve-qwen.sh` or `serve-deepseek.sh`
(both in `/home/deploy`), each = `llama-server -m <first-shard> --alias …
--host 0.0.0.0 --port 8080 -ngl 999 --rpc <pollux cx>:50052 -c 131072 -np 8
--cache-type-k/v q8_0 -fa on --jinja` — 128K ctx, 8 parallel slots, tool
calling on (verified live 2026-08-07Z).

    ssh castor 'ln -sf serve-deepseek.sh serve-current.sh && sudo systemctl restart llama-server'

One cold load; **only one model fits in the ~232 GB combined at a time.** Cold
load ≈ 5–7 min (reads ~193 GB off NVMe + distributes); once up it stays
resident.

## precis LLM router wiring (2026-08-07Z; all DB config, no code)

- llm card ref 196911, `model_id=qwen3-235b-a22b-2507` (= the server `--alias`);
  `served_by` → the castor endpoint under `/v1`; per-node `max_parallel`
  melchior 4 / caspar 2 / balthazar 2 (= the server's `-np 8`);
  `resource_slots` seeded.
- `app_settings` `llm.chain.big` = **local spark rung first, `z-ai/glm-5.2`
  cloud fallback** (`openai_tools` both). Saturation/outage fails over
  automatically; revert/edit via the Models/Services tab chain editor or the
  `app_settings` row.
- First organic dispatch verified 2026-08-07Z 09:15Z (quest_tick → qwen3-235b).
- **A model switch on castor (new `--alias`) needs the card's `model_id` /
  `served_by` and the chain rung updated to match.**

## Model library (`/home/deploy/models`, ~3.5 TB free per box)

Downloaded with the `hf` CLI and `HF_XET_HIGH_PERFORMANCE=1` in the
`/home/deploy/hfenv` venv (a blackholed CDN:
[`hf-cdn-blackhole-prefetch`](./hf-cdn-blackhole-prefetch.md)).

- `qwen3-235b-a22b-2507/Q6_K/` — Qwen3-235B-A22B-Instruct-2507 Q6_K, 193 GB
  (unsloth), near-lossless MoE 22B active. **Default/active.**
- `deepseek-v4-flash-0731/UD-Q8_K_XL/` — DeepSeek-V4-Flash-0731 UD-Q8_K_XL,
  162 GB (unsloth), MoE. ("Flash High" is a runtime reasoning-effort setting,
  not a separate weight file.)

## Build

llama.cpp is built from source on each box; identical hardware → pin the
**same commit** on both. `/home/deploy/build-llama.sh`:
`cmake -B build -DGGML_CUDA=ON -DGGML_RPC=ON -DCMAKE_CUDA_ARCHITECTURES=121-real
-DLLAMA_CURL=ON -DGGML_NATIVE=ON`, then `-j20`. CUDA 13.0 is already on the
image at `/usr/local/cuda` (add its `bin` to PATH — `nvcc` isn't on PATH by
default). GB10 = compute capability **12.1 / sm_121**. Binaries in
`build/bin/` (the RPC server is `ggml-rpc-server`, not `rpc-server`); run with
`LD_LIBRARY_PATH=build/bin`.

## Model-size ceiling

Two Sparks ≈ **220 GB weights** (232 GB combined − KV/OS headroom; each GB10
~116–123 GB usable unified memory). Tops out at the **235B–671B MoE class**
(Qwen3-235B Q6, DeepSeek-V3.1 671B Q2-XL). **Kimi K3 does not fit at any
quant**: 2.8T params, smallest GGUF is UD-IQ1_S at 594 GB (1.6-bit), needing
~6 Sparks; a decent Q2 (~861 GB) needs ~9; even 3 Sparks (~348 GB) fall ~1.7×
short. It wants a ~1 TB unified-memory box or a ~9-node cluster.

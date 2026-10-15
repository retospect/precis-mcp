# vllm role

Runs one vLLM OpenAI-compatible server (one model, fully resident) as a
systemd-managed docker container on a single GB10 host. Playbook:
`playbooks/49-vllm.yml`, targeting inventory group `vllm_serving` (add the
host under that group in your inventory). Spec:
`docs/backlog/vllm-per-node-serving.md`.

The unit restarts the container on failure; the role then waits (bounded,
default 25 min) for `GET /health`. Model load measured 505-861 s plus engine
init; two restarts outran the earlier 15 min cap.

Set `vllm_model_dir` and `vllm_tiktoken_dir` in the overlay's host vars for
the serving host: the defaults name `/srv/models/...`, and a host that keeps
weights under the deploy user's home fails the model-dir check until they are
passed (`-e vllm_model_dir=... -e vllm_tiktoken_dir=...`). From a worktree,
pass the main checkout's `--vault-password-file` and overlay inventory, plus a
layered extra inventory that puts the host in `vllm_serving`.

`vllm_env` passes container environment. Its default is the GB10 (SM121)
workaround `VLLM_MARLIN_USE_ATOMIC_ADD=1`: without it the stock image's
Marlin MXFP4 MoE kernel races and gpt-oss emits a stray leading `!` with
nondeterministic output at temperature 0 (vllm-project/vllm#37030; seen on
castor at every window, 2026-10-12/13). The env alone did NOT fix v0.20.1
(2026-10-13); the working stack is the `sglang` role
(`playbooks/49a-sglang.yml`, verified sound 2026-10-14). Keep this role for
the day a fixed vLLM image (`cu130-nightly` or the NGC track) is pullable. `vllm_max_model_len` defaults to 16384 (the Slice 0 setting);
`scripts/claude-local` needs more (a Claude Code first request exceeds 16k),
see `docs/backlog/claude-code-on-local-serving.md`.

## Prerequisites on the host

- docker with the nvidia runtime configured (no role does this; the role
  fails early if `docker info` lists no `nvidia` runtime).
- Model weights at `vllm_model_dir`; for gpt-oss also
  `o200k_base.tiktoken` and `cl100k_base.tiktoken` in `vllm_tiktoken_dir`
  (vLLM fetches them from the internet otherwise, and the container cannot).
- The image. The role pulls only when it is absent, with a per-attempt
  `timeout` and bounded retries (Docker Hub TLS-times-out from the LAN). A
  pre-pulled or `docker save | ssh | docker load`ed image skips the pull.

## Models

gpt-oss 120B MXFP4 (defaults):

```yaml
vllm_model_dir: /srv/models/gpt-oss-120b
vllm_served_model_name: gpt-oss-120b
vllm_tool_call_parser: openai
vllm_tiktoken_dir: /srv/models/tiktoken
```

Nemotron 3 Super NVFP4 override (one block in group_vars):

```yaml
vllm_model_dir: /srv/models/nemotron-3-super-nvfp4
vllm_served_model_name: nemotron-3-super
vllm_tool_call_parser: qwen3_xml
vllm_tiktoken_dir: ""
vllm_extra_args: ["--trust-remote-code"]
```

Both ran at `--gpu-memory-utilization 0.80 --max-model-len 16384
--max-num-seqs 64` (the default sizing; the server holds 64 streams, a
separate feedback controller holds about 32). `vllm_bind_addr` defaults to
`0.0.0.0`, as llamacpp does; set it to the host LAN address to narrow. Health probes use that address; wildcard
bindings use the corresponding loopback address. IPv6 addresses are supplied
unbracketed and rendered with brackets in Docker port mappings and URLs.

## Exclusive host

The serving host is exclusive to this service. GPU science lanes (aizynth,
dft, alphafold, autocatpath, ...) must not be scheduled on it: they contend
for the same unified memory. Keep it out of those capability groups in the
topology map. This role changes no worker config.

One serving stack per host: `vllm_supersedes_service` (default `sglang`)
names a unit the role stops and disables before starting vLLM, and the
sglang role does the reverse, so a reboot never brings both up on the same
port and GPU memory.

## Next steps (not in this role)

- Register the `llm` card (`source="static"`) and the `resource_slots`
  capacity row by hand; card `max_parallel` and slot capacity move together,
  and a redeploy clears a hand-set `served_by` (Known footguns in the spec).
- Router `chain_override` rung in front of cloud.
- `/metrics` scraping (preemptions, cache usage).

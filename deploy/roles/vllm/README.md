# vllm role

Runs one vLLM OpenAI-compatible server (one model, fully resident) as a
systemd-managed docker container on a single GB10 host. Playbook:
`playbooks/49-vllm.yml`, targeting inventory group `vllm_serving` (add the
host under that group in your inventory). Spec:
`docs/backlog/vllm-per-node-serving.md`.

The unit restarts the container on failure; the role then waits (bounded,
default 15 min) for `GET /health`. Model load measured 505-641 s.

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

## Next steps (not in this role)

- Register the `llm` card (`source="static"`) and the `resource_slots`
  capacity row by hand; card `max_parallel` and slot capacity move together,
  and a redeploy clears a hand-set `served_by` (Known footguns in the spec).
- Router `chain_override` rung in front of cloud.
- `/metrics` scraping (preemptions, cache usage).

# sglang role

Runs one SGLang OpenAI-compatible server (one model, fully resident) as a
systemd-managed docker container on a single GB10 host. Playbook:
`playbooks/49a-sglang.yml`, targeting inventory group `sglang_serving` (add
the host under that group in your inventory). This is the stack that serves
soundly on the GB10 (SM121): the stock `vllm/vllm-openai:v0.20.1` image's
Marlin MXFP4 MoE kernel races on SM121 — flat first-token logits, stray
token-0 `!`, nondeterministic at temperature 0, at every context window
(vllm-project/vllm#37030, verified on the serving host 2026-10-12/13). Keep the
`vllm` role for the day a fixed image is pullable.

Verified on the serving host 2026-10-14 (`sglang-trial` container, these
defaults then at `--mem-fraction-static 0.86` / `--max-running-requests
64`):
byte-identical temperature-0 replies at 1,009 / 16,369 / 66,481-token
prompts with a planted needle, clean tool calls
(`--tool-call-parser gpt-oss`), and a native `/v1/messages` that accepts
Claude-Code-shaped requests — so `scripts/claude-local` needs no
`anthropic-shim` against this stack. KV pool: fp8_e4m3 measured ~211k
full-attention + ~169k sliding-window tokens at 0.86 (smaller at the
shipped 0.80); the bf16 default fit only ~37k, below one max-length
request. The same trial hung the host under load that afternoon
(scheduler 100% CPU, gr485635) — hence 0.80 / 32.

Two load-time traps on unified memory, both encoded in the defaults:

- `--mem-fraction-static` counts against the WHOLE box (weights + KV +
  graphs — GB10 memory is unified CPU/GPU). 0.80 leaves ~24 GiB for the
  OS; the earlier 0.86 sizing hung the serving host under load on
  2026-10-14 (gr485635), so do not raise it while anything shares the
  box. `--max-running-requests` is likewise capped at 32 (the feedback
  controller's level); 64 was the other half of that hang.
- The default weight loader stages ~41 GiB of host buffers and trips
  earlyoom (10% MemAvailable) at shard 14/15. The `runai_streamer` loader
  with a 4 GiB `memory_limit` plus `--weight-loader-drop-cache-after-load`
  holds the peak under it — do not drop these flags, or exempt the unit
  from earlyoom instead.

Set `sglang_model_dir` and `sglang_tiktoken_dir` in the overlay's host vars
for the serving host: the defaults name `/srv/models/...`, and a host that
keeps weights under the deploy user's home fails the model-dir check until
they are passed (`-e sglang_model_dir=... -e sglang_tiktoken_dir=...`).
From a worktree, pass the main checkout's `--vault-password-file` and
overlay inventory, plus a layered extra inventory that puts the host in
`sglang_serving`.

`sglang_env` passes container environment. `sglang_supersedes_service`
names a unit to stop+disable first (default `vllm`) — one serving stack
per host.

## Prerequisites on the host

- docker with the nvidia runtime configured (no role does this; the role
  fails early if `docker info` lists no `nvidia` runtime).
- Model weights at `sglang_model_dir`; for gpt-oss also
  `o200k_base.tiktoken` and `cl100k_base.tiktoken` in `sglang_tiktoken_dir`.
- The image. The role pulls only when it is absent, with a per-attempt
  `timeout` and bounded retries (Docker Hub / nvcr.io TLS-time-out from the
  LAN, seen 2026-10-13). A pre-pulled or `docker save | ssh | docker
  load`ed image skips the pull — `lmsysorg/sglang:latest-cu130` is already
  on the serving host (the Slice 0 arm image).

## Models

gpt-oss 120B MXFP4 (defaults):

```yaml
sglang_model_dir: /srv/models/gpt-oss-120b
sglang_served_model_names: [gpt-oss-120b, openai/gpt-oss-120b]
sglang_reasoning_parser: gpt-oss
sglang_tool_call_parser: gpt-oss
sglang_tiktoken_dir: /srv/models/tiktoken
```

A different model changes the name(s), both parsers (drop them for a
non-reasoning/non-harmony model — empty string omits the flag), and
`sglang_tiktoken_dir: ""` (no harmony vocab mount).

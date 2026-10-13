---
status: in-progress
pillar: local-compute
prio: high
owner: local-compute
---

# Claude Code sessions on local or cheap serving

Filed 2026-10-12 from Reto's ask (chat): Claude credits are running out;
he wants more coding assist, cheaply, and asked about Kimi and Antigravity.

## Motivation / why

Castor serves gpt-oss 120B on vLLM (32 slots) and nothing routes to it
([local-compute](threads/local-compute.md#resume)). Claude Code accepts any
Anthropic-format endpoint via `ANTHROPIC_BASE_URL`; vLLM speaks the OpenAI
format, so one translating proxy makes castor a zero-marginal-cost backend
for the Sonnet-class fleet roles (coder, tidy, test-runner, documenter,
worker sessions). The Opus design-review threads in
`.claude/fleet/threads.tsv` and the main loop stay on Claude.

## In scope

- No proxy: probed 2026-10-12, castor's vLLM v0.20.1 answers
  `/v1/messages` natively (HTTP 200). What is needed instead: a
  `--served-model-name` alias (or `ANTHROPIC_MODEL`) so the names Claude
  Code sends resolve, and `--max-model-len` raised from 16384 (a Claude
  Code turn with CLAUDE.md and tool schemas does not fit) traded against
  `--max-num-seqs` on the GB10's memory.
- A per-session env recipe (`ANTHROPIC_BASE_URL`, auth token) so a worker
  worktree can start on local serving without touching the user's settings.
- A measured comparison on real fleet tasks: a gripe batch run on local
  versus Sonnet, pass rate at the ship gate and wall time.
- A fallback plan for when castor is in a science lane: a cheap
  Claude-Code-compatible cloud plan (Kimi Code, GLM, OpenCode Go), with
  the vendor price verified at purchase time.

## Built 2026-10-12

- `scripts/claude-local`: resolves castor from the overlay
  (`scripts/lib/node-host.sh`), starts `scripts/anthropic-shim.py` on
  loopback, maps every model slot to the served model, execs `claude`.
- `scripts/anthropic-shim.py`: Claude Code sends hook output as a
  `system`-role turn inside `messages` (the SessionStart hook output was
  32k chars in the captured request) and calls `count_tokens`; vLLM's
  `/v1/messages` accepts neither. The shim rewrites system turns to tagged
  user turns, merges adjacent same-role turns, estimates `count_tokens`,
  streams the rest through. Verified against castor: rewritten turn
  accepted, SSE passthrough, count_tokens.
- `deploy/roles/vllm/defaults/main.yml`: `vllm_max_model_len` 16384 →
  131072. Claude Code's first request was ~50k chars of messages plus 30
  tool schemas, so nothing works until castor restarts with the new value
  (Reto authorized the deploy 2026-10-12; run by Reto, the session's
  classifier refused the production deploy step).

**Restart result 2026-10-12 ~20:50Z (window 131072, model load 861 s):**
`scripts/claude-local -p` answered OK once, then castor went bad: identical
temperature-0 requests returned different text, one reply reasoned about a
prompt it was not sent, and both `/v1/messages` and `/v1/chat/completions`
with a tool hit `unexpected tokens remaining in message header` (the model
emitted a reserved harmony token). The OpenAI path without tools still
answered. Same image, flags and model as before; the only change was
`--max-model-len` 16384 → 131072, so the next try is 65536, then 32768,
reading the container log for the attention backend and KV-cache lines
first. The summarise op was reverted to melchior's glm at 20:58Z so no
corrupted summaries land. The session could not read castor's container
log (SSH and ansible ad-hoc both refused by the harness); Reto runs it.

**Log read 21:05Z (Reto-authorized ansible ad-hoc):** backend TRITON_ATTN,
MARLIN mxfp4 MoE, KV cache 814,522 tokens (28.4 GiB), 6.2x concurrency at
131072; no warnings beyond the harmony error. Two more findings: (1) prod
workers that routed to castor asked for `openai/gpt-oss-120b` and got 404
(`The model ... does not exist`), because vLLM serves only
`gpt-oss-120b`; so the summarise flip never reached the model and no
corrupted summaries were written. The role now passes
`vllm_served_model_aliases` so the card name is served too. (2) castor's
own worker sends `glm-4.7-flash` local calls to its loopback vLLM and 404s
(falls through to cloud); pre-existing, filed here as a note.

**65536 redeploy, 21:33Z:** health wait outran the 15-minute cap (924 s)
but the server came up; output was WORSE: every reply degenerate from the
first token on both API paths, even without tools, and raw `/v1/completions`
answers fluently but ignores the prompt and starts with `!` (token 0), at
temperature 0 and nondeterministic. So the window is not the cause. The
host read shows the likely one: 121 GiB unified memory, the engine holding
98 GiB at `--gpu-memory-utilization 0.80`, 1 GiB free, 1 GiB of swap in
use, only vLLM on the GPU. Next try: 65536 at 0.70 with 120 health retries.
If still bad, `--disable-hybrid-kv-cache-manager` and
`--no-enable-prefix-caching` (gpt-oss alternates sliding-window layers),
then `--enforce-eager`.

**65536 at 0.70, 22:30Z:** unit restarted twice during the playbook (health
cap outran again at 1230 s; load is ~33 s per shard, 15 shards, then
compile), then healthy with 45 GiB available. Still unsound: raw
completions are fluent but token 0 (`!`) is injected at random positions,
different on each identical temperature-0 run; chat paths return no
content. Memory pressure is ruled out. Signature = intermittent kernel
fault (Triton attention or Marlin MXFP4 MoE on the GB10) at the larger
window's batch shapes. Decision: roll back to the known-good 16384 (now
with the alias) to restore the summarise lane, and take the Claude Code
window up as a planned experiment: `--enforce-eager` at 32768, then a newer
vLLM image, then llama.cpp (serves the Anthropic Messages API natively and
needs no harmony parser) as the alternative stack for the helper role.

Open after the restart: a non-stream reply with `max_tokens` 20 came back
with empty content and `stop_reason: max_tokens`, so gpt-oss's reasoning
channel may be eating the budget or vLLM's adapter may drop final text;
check a `max_tokens` 500 reply shows text before the first real helper run.

## Explicitly NOT in scope

- Antigravity or other IDE harnesses: they do not run this repo's hooks
  (rtk, secret gate, git -C guard, encoding tests), so fleet sessions do
  not move there.
- Routing the product's LLM chains; that is the local-compute thread's
  own item.

## Acceptance criteria

- A worker session starts on castor with one documented env line and
  lands a gripe fix through the normal ship gate.
- The comparison numbers are in the thread file before any paid plan is
  bought.

## Open questions

- Which proxy: LiteLLM or claude-code-router (role-based model mapping).
- Whether subagent `model:` fields in `.claude/agents/*.md` need a mapped
  alias, since Claude Code only accepts Anthropic model names there.

**Rollback to 16384, 2026-10-13 10:30Z: still unsound; root cause found.**
Redeployed at 16384/0.70 (healthy at 10:25Z). Same corruption: raw
completions nondeterministic at temperature 0, stray leading `!`, the
Anthropic path 500s in the harmony parser. The window was never the cause.
`logprobs` on a raw completion shows the stray `!` (token 0) at -12.21 tied
with `"` at -12.21: flat first-token logits. That is the signature of
vllm-project/vllm#37030 and the NVIDIA forum thread "SM121 4 bugs causing
`!` output": the stock image's Marlin MXFP4 MoE kernel has a shared-memory
race on the GB10 (SM121), and the CUTLASS FP4 device gate false-positives
on SM12x. `llm_call_log` has zero rows for `gpt-oss-120b` ever, so there
was no known-good state; Slice 0 measured throughput, not output. Kernel
log shows NVRM `NV_ERR_NO_MEMORY` bursts at each container start (not at
the 10:16Z one), so memory pressure is a second, separate issue.
Fix order: (1) role now passes `VLLM_MARLIN_USE_ATOMIC_ADD=1` (`vllm_env`),
redeploy and re-probe; (2) if still bad, move `vllm_image` to the CUDA 13
track vLLM recommends for Spark (`vllm/vllm-openai:cu130-nightly`, pinned
by digest, or NGC `nvcr.io/nvidia/vllm:26.05-py3`) where the SM121 fixes
landed; (3) the window experiment above then runs on a sound server.
Spark guidance seen on the way: stay on driver 580.x (590 has a CUDA-graph
deadlock), do not use `--enforce-eager` (halves throughput), keep
`--gpu-memory-utilization` at or below 0.87 on unified memory.

**Atomic-add env, 2026-10-13 11:47Z: not enough on the stock image.**
Redeployed with `VLLM_MARLIN_USE_ATOMIC_ADD=1` (confirmed in the unit and
the engine log still picks MARLIN MXFP4 + TRITON_ATTN). Logprob probe: 4 of
4 raw completions still start with token 0 `!`; raw output still
nondeterministic at temperature 0. The forum's fix set also needs the
SM121 source patches (CUTLASS FP4 device-gate false positive, Marlin
thread race), which v0.20.1 does not carry. Next: the image track.
Image track, 2026-10-13 12:10Z: neither castor nor the dev Mac could reach
Docker Hub or nvcr.io (TLS timeouts), so no new vLLM image is pullable
today. Zero-pull alternative already on castor: `lmsysorg/sglang:latest-cu130`
(v0.5.21, the Slice 0 SGLang arm; NVIDIA's showcased Spark stack). SGLang
serves a native `/v1/messages` with `count_tokens` and documents Claude Code
as a client, which would also retire `scripts/anthropic-shim.py`. Trial:
stop the vllm unit, run the image on port 8000 with `--served-model-name
openai/gpt-oss-120b --context-length 131072 --mem-fraction-static 0.7
--reasoning-parser gpt-oss --tool-call-parser gpt-oss`, re-run the probes.
If sound, write an `sglang` role (or a server switch in the vllm role) and
retire the Marlin workaround.
SGLang trial, 2026-10-13 12:35Z: two launches at `--mem-fraction-static
0.8` died with exit 137 during shard loading. Not SGLang: castor's
`earlyoom` (hardening role, `-m 10,5 -s 10,5`) SIGKILLed the launcher when
MemAvailable fell to 6.9% with swap exhausted; on the GB10 the "GPU"
reservation is host memory, so a 0.8 static fraction plus the loader's
staging exceeds the box. Slice 0 ran at 0.8 before earlyoom was installed.
Any serving role on a GB10 must size memory so MemAvailable stays above
earlyoom's 10% through model load (vLLM at 0.70 leaves ~30 GiB and
survives). Third launch at 0.6.
Third launch at 0.6 died the same way; fourth with sequential shard loading
(`--model-loader-extra-config '{"enable_multithread_load": false}'`) got to
14 of 15 shards: scheduler RSS 41 GB on top of the unified-memory weights,
MemAvailable 8%, earlyoom fired at 12:48Z. The static fraction is not the
lever; the loader's host-side staging peaks at the end of load and frees
after. Fifth launch pauses earlyoom for the load window only (restored by
a guard on castor itself). If SGLang is sound once up, the role needs
either that guard or an earlyoom exemption for the serving unit.

---
status: idea
title: Local model as batch function-writers behind a mechanical gate
pillar: local-compute
blocked-by: vllm-per-node-serving
---

# Local model as batch function-writers behind a mechanical gate

Reto, 2026-10-04: could the big model on a Spark write code, given a proper
harness? Answer from that discussion: yes for function-sized, test-gated
patches, run as single-shot calls from a script. Anything larger costs more
in review than it saves.

## Shape

A script, no agent loop and no tmux pane:

1. Assemble one prompt: the spec, the target function, the signatures it
   depends on, the test.
2. One model call per attempt, several attempts in parallel.
3. Apply the patch, run ruff, mypy and the targeted test. Retry once or
   twice with the error text.
4. Survivors go to a Codex or `coder` foreman, which batches them into one
   branch per work list ([per-agent-green-is-not-integrated-green](per-agent-green-is-not-integrated-green.md)).

Single-shot is the point. Slice 0 measured gpt-oss 120B on vLLM at 16k
context: 33.6 tok/s per stream at 1 stream, 18.6 at 8, 6.7 at 64, ceiling
about 450 tok/s in total ([vllm-per-node-serving](vllm-per-node-serving.md)).
An agent loop needs several times that context and re-sends it every turn;
a single-shot call fits in 16k and can use every stream.

## Workloads where the check already exists

Writing a spec plus a test for a function costs a frontier model about what
writing the function costs. Local pays only where acceptance is mechanical:

- surviving mutants from `scripts/mutate-diff` (the new test passes on the
  original and fails on the mutant);
- diff coverage on existing code;
- the `store: Any` and positional-mapper conversions in
  [codereview-residuals](codereview-residuals.md) (mypy is the check);
- one edit repeated across many call sites.

## Limits

- **Nothing serves today.** Read 2026-10-04: no LLM server on castor,
  pollux or spark; castor and pollux memory-full with other work. This
  rides on the standing castor server, not a bring-up of its own.
- **Test compute.** Every candidate needs a test run and the gate containers
  on the dev Mac are contended. The idle spark box could run them under the
  ad-hoc-compute ruling in [llm-capacity-plan](llm-capacity-plan.md).
- **No quality number for code.** The km-8 check is a taxonomy task.
- Local reviewing local shares one model's blind spots. Keep it to
  scope and spec checklists.

## First step

Replay 5 to 10 small landed commits that have tests: give each worker the
spec and tests, hide the implementation. Score first-try and best-of-4 pass
rate for the local model, Codex and `coder`. Then run the script on mutation
survivors and record accepted patches per hour and foreman tokens per
accepted patch. That number decides whether the foreman layer is built.

Unverified: that codex-cli 0.160.0 and vLLM agree on a wire protocol, and
prompt-processing speed at long context on a GB10. Neither matters for the
single-shot script; both matter if an agent loop is tried later.

test: on a fixture repo with one failing test and a stub model that returns
a fixed patch, the script applies it, runs the gate, and reports accepted.

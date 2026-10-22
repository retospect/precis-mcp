---
status: draft
title: Castor agent lane — gpt-oss-120b as a builder/helper beside Devin and Claude
pillar: local-compute
prio: high
---

# Castor agent lane

Filed 2026-10-22 from Reto's ask (chat desk): Devin (SWE-2 HIGH) is
unlimited today, the weekly Claude allotment runs out, and castor sits
mostly idle serving `openai/gpt-oss-120b` on SGLang (131,072-token window,
32 running requests, ~3 s per short call, $0). "What do we need to do to
make it awesome?" — castor as a third builder/helper lane the coordinator
can launch when Devins run out or the task is too mechanical to spend a
Devin on.

What already exists (do not rebuild): `scripts/claude-local` runs a real
Claude Code session against castor through `scripts/anthropic-shim.py`
(chat mode on SGLang; multi-step tool use verified live and deployed in
r84, gr500583); the product router reaches castor through a `llm.op.<source>`
chain with `placement: local` (`precis.utils.llm.operations.resolve_op_chain`,
`router._served_local_rung`, slot accounting in `local_serving.acquire`);
the gardener proposer runs that way today (me502337); the MEDIUM replay
eval (`docs/reference/castor-medium-load-eval.md`) measured 86-100 %
agreement with the cloud rung on verify/reground/locate at 3-8 s per call.
The single-shot batch shape is specced in
[local-coder-batch-harness](local-coder-batch-harness.md); the proxy
question was opened in
[claude-code-on-local-serving](claude-code-on-local-serving.md) and is
answered here.

## Motivation / why

Three lanes, three cost curves. Claude is the scarce one (weekly quota,
also the only one that may touch design-review threads). Devin is free
today but not forever, and it is a frontier-class SWE agent that should
not be spent on lint. Castor costs electricity. The coordinator needs a
lane whose marginal cost is zero for the work that is mechanical enough
to be verified by the gate rather than by a reader, and a fan-out lane
for the work that is embarrassingly parallel (per-file review, per-gripe
triage) where 32 slots beat one careful reader.

The lane only pays if three things hold: the harness runs our guards
(the repo is public; hooks are the enforcement), every landing goes
through the normal gate plus a Claude read, and the pass rate is measured
on real closed gripes before any task type is trusted.

## 1. Harness

Two harnesses, one per shape of work. Both are in the repo already.

**Agent lane: Claude Code via `scripts/claude-local`** (recommended for
anything that edits a worktree).

| Option | Tool calling with gpt-oss | Worktree isolation | Our hooks and guards | MCP access | Verdict |
|---|---|---|---|---|---|
| Claude Code + `scripts/claude-local` (`ANTHROPIC_BASE_URL` → `anthropic-shim.py` chat mode → SGLang `/v1/chat/completions`) | Works since a04f02ad7: shim folds `system` into the first user turn (any system-role message makes gpt-oss end its turn after reasoning on SGLang's harmony path, gr500583 c1), maps `tool_use`/`tool_result` ↔ `tool_calls`/`tool`, re-emits SSE. SGLang parses calls with `--tool-call-parser gpt-oss` ([SGLang gpt-oss cookbook](https://docs.sglang.io/cookbook/autoregressive/OpenAI/GPT-OSS)). | `claude -w NAME`, same as every builder; `scripts/fleet-authority apply` writes the tree's `settings.local.json` and Claude Code honours it | All of them: rtk, secret gate, `git -C` guard, worktree shell guard, read-dedup, PreToolUse classifiers — they are Claude Code hooks | Session precis MCP (R endpoint), same config as a Claude builder | **Use it.** The only option where tier R/W is enforced by the same files, and `fleet_panes` already parses the pane. |
| Claude Code + LiteLLM proxy | Same translation as the shim, more surface; LiteLLM's `/v1/messages` → chat-completions path needs `use_chat_completions_url_for_anthropic_messages` ([LiteLLM: Claude Code with non-Anthropic models](https://docs.litellm.ai/docs/tutorials/claude_non_anthropic_models)). Anthropic's gateway page warns LiteLLM PyPI 1.82.7/1.82.8 shipped credential-stealing malware ([Claude Code: other LLM gateways](https://code.claude.com/docs/en/llm-gateway)). | Same as above | Same as above | Same as above | No gain over the 400-line stdlib shim we control; a third-party dependency on a public-repo tool path. |
| Claude Code + claude-code-router | Role-based model mapping (background/think/longContext per model) and a transformer per provider; no gpt-oss transformer documented, generic OpenAI-compatible only in forks ([DataCamp CCR tutorial](https://www.datacamp.com/tutorial/claude-code-router)). | Same | Same | Same | Not needed: every slot maps to the one served model (`claude-local` already does that) and the system-message fold is ours to do. |
| opencode | `@ai-sdk/openai-compatible` provider with a `baseURL` ([opencode providers](https://opencode.ai/en/docs/providers)); tool calling via the AI SDK, no harmony-specific handling | Own; not `claude -w`, not locked `pid` for `scripts/inflight` | None of ours run; permissions are its own model | Its own MCP config, not the fleet's R/W endpoints | No. |
| aider | OpenAI-compatible via `OPENAI_API_BASE` + `openai/` prefix ([aider openai-compat](https://aider.chat/docs/llms/openai-compat.html)); edits by diff/whole-file formats, not tools — the most robust edit path for a weak tool-caller | Own | None | None | Only as a fallback edit engine if gpt-oss tool calling proves too flaky; loses the guards. |
| Goose | Custom provider JSON with `base_url` ([Goose custom providers](https://www.mintlify.com/block/goose/api/providers/custom)) | Own | None | Own MCP | No. |
| OpenHands | `openai/MODEL` + custom base URL in its evaluation harness ([OpenHands evaluation harness](https://docs.openhands.dev/usage/how-to/evaluation-harness)); sandboxed runtime | Own container | None | None | No; a container per task is a second workspace system beside [agent-workspace-containers](agent-workspace-containers.md). |
| Own `claude_agent` loop with a provider swap | Full control of the harmony encoding; the only way to use the model's native format | Would need `claude -w` semantics rebuilt | Would need every hook re-implemented | Direct (`.scratch/precis_chat.py` prototype did it over HTTP, gr500583 c1) | For the **batch lane** only (below): a script, not an agent, where hooks do not matter because nothing is edited. |

Tool-calling reliability is the real risk, and it is harness-shaped, not
model-shaped: the harmony-native harness in
[In harmony with gpt-oss](https://arxiv.org/abs/2604.00362) reproduces
OpenAI's 60.4 % SWE-bench Verified (high) while the public Chat
Completions harnesses score 26 %, and the model calls an undefined
`apply_patch` tool hundreds of times because it is a training prior
(the model card's own terminal harness has it; [model card](https://arxiv.org/pdf/2508.10925)).
Consequences for us: keep the shim (it is our harmony adapter), give the
model an `apply_patch`-shaped tool rather than fighting the prior (an MCP
tool in the lane's config, slice 2), cap turns (`--max-turns`) so an
over-exploring run ends, and measure (slice 0) before trusting.

**Batch lane: the product router** (recommended for fan-out that edits
nothing). One `route()` call per item through an `llm.op.castor_lane:<task>`
chain, `placement: local`, `max_tokens` 4000, `reasoning_effort` low or
medium, no cloud fallback (deliberately, like the gardener: a castor
failure must be visible). Slot accounting, `llm_call_log`, failure
classes and the `/placement` view come for free. Caller is a script under
`scripts/castor/` (one per task type), never a pane.

## 2. Task fit

| Castor **owns** (lands via the gate + Claude read) | Castor **assists** (output goes to a Claude/Devin reader) | Stays on Claude/Devin |
|---|---|---|
| ruff/mypy residuals the auto-fixer leaves (`tidy` remit) | first-pass review of a diff, per file (findings ranked for the `reviewer` agent) | architecture, anything in a `design-review: yes` thread |
| tests from a spec where the check is mechanical: mutants from `scripts/mutate-diff`, diff coverage, repro tests for a gripe with a quoted traceback | gripe triage: class, duplicate candidates, owning package, a `bug`-skill bucket proposal | cross-module changes, new seams, handler contracts |
| doc sync after a landed code change (`documenter` remit): docstrings, skill text, backlog delete-on-ship | log and job-transcript mining (`forensics` remit): ranked candidates, evidence quoted | migrations, `safe_fetch.py`, permission settings, authority files |
| one edit repeated across call sites (positional-mapper, `store: Any`, encoding kwargs) | memory/graph candidate proposals (the gardener already does this on castor) | security, anything outward-facing, anything over $25 |
| renames and moves where mypy + the selected tests are the check | summaries of a long session log for the review queue | conflict resolution, rebase repair (gr497896: never in a desk loop) |

The rule behind the split: castor owns a task when a machine check
(ruff, mypy, `scripts/test --selected`, a byte-compare) decides pass or
fail; it assists when a reader decides; it is excluded when the undo is
not a `git revert`.

## 3. Use the concurrency

32 running requests is the slot count; the KV pool is the binding
constraint for agent sessions. The role's fp8 pool measured ~211k
full-attention + ~169k sliding-window tokens at 0.86 and is smaller at the
shipped 0.80 (`deploy/roles/sglang/defaults/main.yml`), so one 131k agent
context already takes most of it — eight agent sessions at 20-30k context
each fit; thirty-two do not. Patterns, ordered by fit:

- **Per-file review** (batch lane): one call per changed file with the
  file, the diff hunk and the module docstring; ~N files in parallel, 3-8
  s wall; findings merged and ranked by a Claude `reviewer` call that
  reads only the merged list. Prefix: the review rubric, byte-stable.
- **Per-gripe triage and dedup** (batch lane): one call per open gripe
  with the gripe body plus the top-10 `search(kind='gripe')` neighbours;
  output `{class, duplicate_of?, package, bucket}`; a Claude pass accepts
  or rejects per row. Runs over the 80 open gripes in under a minute.
- **Parallel test-fix attempts with selection** (batch lane + gate): for a
  red test, N=4 single-shot patch attempts at temperature 0.7 against
  the failing test, the target function and its callees from
  `get(kind='python')`; each applied in a scratch copy, run through ruff,
  mypy and the one test; survivors (usually 0-2) go to one Claude
  judge; the winner lands as a commit by a tier-R castor builder or the
  coordinator. This is [local-coder-batch-harness](local-coder-batch-harness.md)
  with a real server.
- **Best-of-N for a spec'd function** (same machinery, N=8, selection by
  the spec's test plus a Claude judge on the survivors).
- **Parallel agent sessions** (agent lane): a wave of up to 8 castor
  builders, one gripe each, R tier, `--max-turns 60`. Budgeted, see §6.

Fan-out writes nothing to prod; every output is a candidate for a reader
or a gate.

## 4. Quality controls

**Gates.** A castor builder never lands with `scripts/ship --quick` on
code (the W-tier allowance for `--quick` is for Claude/Devin builders,
authority.md v2); it commits in its tree and the coordinator or release
owner lands with `scripts/ship --checked` (selected tests via
`scripts/impacted-tests`) after a Claude read: `reviewer` agent on the
diff, or Opus for anything touching a handler contract. A red
`--checked` goes back to the castor builder once with the failure text;
a second red goes to Devin or Claude, never a third castor try.

**Authority.** Tier R by default: commits only, R MCP endpoint, no ship.
Tier W only for named task types after their pilot pass rate is in (§4
below): the first candidates are ruff/mypy residuals and doc sync, where
the undo is one revert and the gate is the check. The tier is applied by
`scripts/fleet-authority apply` on the tree exactly as for a Claude
builder — the footer text grants nothing; the settings file does.

**Scoring and measurement.**

- Pilot set: 10 closed gripes whose `resolved: <sha>` comment names a
  single-commit fix on main that touched ≤3 files and is not a migration
  (`search(kind='gripe', status='done', q='resolved:')`, filter by the
  commit's diffstat). Reproduce the pre-fix state in a worktree by
  reverting the fix commit, brief the castor builder with the gripe body
  only, and compare.
- Pass = the builder's commit makes `scripts/ship --checked`'s selected
  tests green AND a blind Claude reviewer (sees both diffs, not which is
  which) rates the castor diff acceptable to land. Partial = green but
  rejected by the reviewer. Record per gripe: pass/partial/fail, turns,
  wall time, prompt tokens, whether an undefined tool was called.
- Cost saved: the Claude tokens the same gripe cost when a Claude
  builder fixed it (the fleet state's `.prompt` and pane `ctx` are not
  retained; use the `cost_usd` envelope from the original session where
  `llm_call_log` has it, else the median builder-session cost from
  `docs/reference/castor-medium-load-eval.md`'s method on 5 recent
  claude-builder gripes).
- Acceptance thresholds for trusting a task type: pass ≥ 7/10 and no
  fail that touched a file outside the brief's list. Below that, the
  task type stays "assist".
- Both lanes log to `llm_call_log` (the batch lane natively; the agent
  lane through a shim counter written per request, slice 1) so
  `get(kind='llm', id='/placement')` shows castor-lane share by day.

## 5. Context: what to feed 131k

- **What Claude Code already sends:** CLAUDE.md (and the `@RTK.md`
  include), the tool schemas (~30), the SessionStart hook output (now a
  counts line, gr499042) and the brief. The first request was ~50k chars
  in the 2026-10-12 capture — a quarter of the window, repeated every
  turn. That is the prefix to cache.
- **RadixAttention.** SGLang caches by exact token prefix; a byte-stable
  system prompt and tool list across turns and across sessions gives the
  high hit rates the SGLang paper reports, and any per-turn variation at
  the head (timestamps, reordered tools) drops to zero
  ([SGLang paper](https://arxiv.org/pdf/2312.07104)). The shim folds
  `system` into the first user turn, so the cacheable prefix is the
  folded block; keep the fold deterministic (same ordering, no clock) and
  put the brief after it. Measure with `--enable-metrics` (slice 1).
- **Repo map.** Not a file dump: the brief names files and anchors
  (gripe-fleet step 2), and the builder reads the structure through the
  python kind (`get(kind='python', id='main::<qualname>')` for signature
  + callers + callees) and `scripts/coderef`, exactly as a Claude builder
  does. `docs/codebase.md` is 1 read; the owning package docstring is
  the second. A castor brief adds an explicit "read these first, nothing
  else" list because the model over-explores (gr500583 c1).
- **Skills.** `get(kind='skill')` on demand; the brief names the one or
  two that apply. AGENTS.md is read in full once (builder-workflow.txt
  says so); it is ~the same size for every builder.
- **Budget per turn.** Hard cap the session at 100k by `--max-turns`
  and by telling the builder to summarise and restart (a fresh session
  with the summary) past 80k, since the KV pool is shared (§3).

## 6. Ops

**Sharing castor.** Today's castor consumers: the gardener proposer
(`llm.op.graph_garden:propose`, 2-hourly), chase verify batches, the
summarise op when it is flipped local, the MEDIUM eval reruns, and the
survey_extract trial. The agent lane must not starve them.

- Priority classes: SGLang has `--enable-priority-scheduling` with
  `--schedule-policy priority`, a per-request priority integer, and a
  `--priority-scheduling-preemption-threshold` (default 10)
  ([SGLang server arguments](https://docs.sglang.io/advanced_features/server_arguments.html)).
  Proposal: product ops (gardener, verify, summarise) priority 20, the
  batch lane 10, agent sessions 0, with preemption left on so a product
  burst retracts an idle agent turn rather than queueing behind it. The
  rung grammar gains a `priority` key the local wire forwards; the shim
  sets 0.
- Slot budget: the `resource_slots` row `llm:openai/gpt-oss-120b` stays
  `max_parallel` 32 for the router; the agent lane is capped at 8
  concurrent sessions by `fleet-wave launch --castor` (refuses a ninth
  with the live count), and the batch lane acquires through
  `local_serving.acquire` with a per-script concurrency of 16, so product
  calls always find ≥8 free.
- The KV pool, not the slot count, is what the product calls compete
  for: a long-context agent turn plus a 66k verify batch can exceed it,
  and SGLang retracts by `--retraction-policy length` by default;
  `priority` retraction (same flag) is the setting to use once priority
  scheduling is on.

**SGLang settings.** Keep the role defaults (parsers `gpt-oss`, context
131072, fp8 KV, 0.80, 32). Add through `sglang_extra_args`:
`--enable-metrics` (Prometheus, scraped by the monitoring role),
`--enable-priority-scheduling --schedule-policy priority
--retraction-policy priority`, and `--max-queued-requests` so an
over-subscribed lane fails fast into the fallback instead of queueing
for minutes. Per request: `reasoning_effort` low for lint/doc/triage,
medium for test-writing and fixes (the rung key already exists — the
MEDIUM eval ran `reasoning_effort: low`); `max_tokens` ≥ 4000 on every
agent turn because reasoning spends it first (a 20-token reply returns
empty, claude-code-on-local-serving.md). Speculative decoding
(`--speculative-algorithm EAGLE3` with the published gpt-oss-120b draft)
is a decode-speed lever to try once registries are reachable; it is an
open question, not a default.

**Monitoring.** `/metrics` (running/queued requests, cache hit rate,
retractions) into the existing Prometheus; `llm_call_log` by
`source LIKE 'castor_lane:%'`; the shim's log (one line per request:
turn, prompt tokens, finish reason, tool names); `fleet-wave stuck-watch`
for panes; the host side is
[castor-resource-observability](castor-resource-observability.md).

**Failure modes, each with its detector:** reasoning-only turn (shim:
finish `stop` with no content after a tool result → the fold regressed);
undefined tool call (shim: tool name not in the request's tool list →
count it, return a tool error naming the available tools); over-exploring
(turn cap); KV retraction storm (metrics: retractions/min); 404 on the
model name after a redeploy (health probe checks `/v1/models` names
`openai/gpt-oss-120b`); earlyoom during a restart (role notes); scheduler
pin at 100 % CPU (gr485635 — the 32/0.80 cap, do not raise).

**Fallback when castor is down.** `scripts/claude-local` exits 2 on a
failed health probe; `fleet-wave launch --castor` refuses and prints
"castor down — launch as --devin or claude". A running castor builder
that loses the endpoint ends its turn with an error; the coordinator
relaunches the same brief on Devin. The batch lane's op chains have no
cloud rung on purpose: a down castor means the fan-out waits, never
silently bills Claude. The only automatic cloud fallback is the one the
caller names in the rung.

## 7. Integration with the fleet

- `scripts/fleet-wave launch NAME --brief FILE --castor` mirrors
  `--devin`: window `castor-NAME` (a third `PREFIXES` entry), the tree
  created by `_ensure_tree`, `fleet-authority apply --tier R` (W only
  with an explicit `--tier W` for a trusted task type), then
  `scripts/claude-local -w NAME --model openai/gpt-oss-120b
  --permission-mode auto --max-turns 60 <prompt>` in the window.
  `fleet_panes` parses it as a `claude` pane; `LAUNCH_PERMISSION_MODE`
  gains `castor: auto` (never `dangerous`: this model gets the
  classifier). `watch-done`, `stuck-watch`, `close` work unchanged.
  `--dry-run` prints the argv. Launch refuses when the castor budget
  (8) is full or the health probe fails.
- **Briefs.** The gripe-fleet brief format, plus three castor lines: the
  explicit read-first file list, the one test command, and "tools you
  may call" (Read, Edit, Bash for `scripts/test`, the python-kind MCP).
  A castor brief never asks for a design decision.
- **Review queue.** A castor builder never files to Reto's queue; it
  sends blockers to the coordinator, who decides whether the task is
  castor-shaped at all. Review of its diff is the coordinator's `reviewer`
  call before landing (§4).
- **Coordinator's pick rule, in order:** (1) design-review thread,
  migration, security, cross-module → Claude. (2) Needs judgment, Devin
  slot free → Devin. (3) In castor's "owns" column with a mechanical
  check, or a fan-out → castor. (4) Devins exhausted and the task is in
  "assist" → castor drafts, Claude finishes from the draft (cheaper than
  Claude from scratch). The rule lives in the gripe-fleet skill once
  slice 3 ships.

## Slices

- **Slice 0 — pilot (R, no new code).** Pick 10 closed gripes per §4,
  revert each fix in a worktree, launch `scripts/claude-local -w` by hand
  with the gripe-only brief and `--max-turns 60`, land nothing. Record
  the table in this file. Acceptance: all 10 run to a terminal state; per
  gripe pass/partial/fail, turns, wall, prompt tokens, undefined-tool
  count; a one-line verdict per task type that appeared; shim log shows
  zero reasoning-only stalls after a tool result.
- **Slice 1 — observability.** `sglang_extra_args` gains
  `--enable-metrics`; the shim logs one line per request and counts
  undefined tool names; the agent lane's requests land in `llm_call_log`
  with `source='castor_lane:agent'`. Acceptance: `/placement` shows the
  castor-lane share; a Prometheus query returns cache hit rate and
  retractions from castor.
- **Slice 2 — `fleet-wave launch --castor`.** The `castor-` prefix,
  budget check, health refusal, `LAUNCH_PERMISSION_MODE['castor']`,
  `claude-local -w`, an `apply_patch`-shaped MCP tool in the lane's
  config, tests beside `tests/test_fleet_wave*.py`. Acceptance:
  `--dry-run` prints the argv; a real launch produces a `castor-x`
  window that `panes status` reports; a ninth launch is refused with the
  live count; a launch with castor unhealthy is refused with the
  fallback line.
- **Slice 3 — batch lane, first task.** `scripts/castor/triage-gripes`:
  per-gripe class + duplicate candidates through
  `llm.op.castor_lane:triage` (local, no cloud rung, `reasoning_effort`
  low, concurrency 16), output a TSV for a Claude accept/reject pass.
  Acceptance: 80 open gripes triaged in under 2 minutes wall; Claude
  accepts ≥ 70 % of the rows unchanged; the gardener's 2-hourly run during
  the batch does not time out.
- **Slice 4 — priority classes.** Role flags for priority scheduling and
  priority retraction; a `priority` rung key forwarded by the local wire;
  the shim sends 0. Acceptance: a 16-wide batch at priority 10 while a
  gardener call at 20 runs: the gardener call's latency stays within 2x
  of its idle latency.
- **Slice 5 — parallel test-fix with selection.** The N=4 attempt script
  from §3 with ruff/mypy/one-test selection and a Claude judge on
  survivors. Acceptance: on 10 red tests from recent gripes, ≥ 4 produce
  a surviving patch; the judge's pick lands green through `--checked`
  for ≥ 3.
- **Slice 6 — tier W for named task types.** After slices 0 and 5
  report ≥ 7/10 on ruff/mypy residuals and doc sync, those two types
  launch at `--tier W` and land their own slice with `--checked`.
  Acceptance: the first three W landings pass the Claude read with no
  finding above "cleanup"; the gripe-fleet skill carries the pick rule.

## Explicitly NOT in scope

- Replacing Claude or Devin on anything in the "stays" column.
- Routing the product's own tiers (`llm.chain.<tier>`) to castor;
  that is [local-rungs-small-medium](local-rungs-small-medium.md) and the
  local-compute thread's items.
- A second serving stack, a second model, or raising 0.80/32 on castor.
- Running castor builders in `dangerous` permission mode.
- OpenHands/opencode/aider/Goose as fleet members (they run none of the
  guards; table in §1).

## Acceptance criteria

- Slice 0's table is in this file with a per-task-type verdict, and the
  coordinator's pick rule is written from it, not from this draft.
- A castor builder launches with one `fleet-wave` command, runs under tier
  R with the same hooks as a Claude builder, and its work lands only via
  `--checked` plus a Claude read.
- The product's castor ops (gardener, verify, summarise) show no
  timeout or latency regression attributable to the lane (slice 4
  acceptance), measured in `llm_call_log`.
- `get(kind='llm', id='/placement')` reports the castor-lane share and
  the Claude tokens the lane displaced (the slice-0 cost column, kept per
  week).

## Target + blast radius

`scripts/fleet-wave`, `scripts/fleet_panes.py`, `scripts/claude-local`,
`scripts/anthropic-shim.py`, `scripts/fleet-authority` (a `castor`
launch kind, no authority change), `deploy/roles/sglang`
(`sglang_extra_args` only, Reto deploys), `precis.utils.llm.router`
(`priority` rung key), `precis.utils.llm.operations` (register
`castor_lane:*` sources), new `scripts/castor/*`, the gripe-fleet skill,
`.claude/fleet/builder-workflow.txt` (castor lines). No migration, no
handler contract change, no prod data writes from the lane itself.

## Risks

- Tool-calling flakiness through Chat Completions: the harmony paper's
  60 % vs 26 % gap is the harness. Mitigations: shim (ours), `apply_patch`
  tool, turn cap, slice-0 measurement before trust. If slice 0 is under
  4/10 on the simplest type, the agent lane is shelved and only the batch
  lane proceeds.
- KV pool contention with product ops (§3, §6): the agent budget of 8 is
  a guess until slice 1 metrics exist; the gardener timeout gripe class
  (gr503581) is the canary.
- Anthropic states it does not support routing Claude Code to non-Claude
  models through a gateway ([Claude Code: other LLM gateways](https://code.claude.com/docs/en/llm-gateway));
  a Claude Code release can change what it sends and break the shim
  (gr500583 was one such). The shim's tests (`tests/test_anthropic_shim.py`)
  and slice 1's stall counter are the early warning; aider is the
  edit-engine fallback, without guards, for batch-lane patches only.
- Public repo: the lane adds scripts and briefs; no hostnames, no IPs,
  the role name `castor` only (as in `docs/runbooks/` and the thread
  file); `tests/test_deploy_tree_no_secrets.py` gates it.
- Reviewer fatigue: 32-wide fan-out can bury the one Claude reader. The
  batch lane's outputs are ranked and capped (top-N per run) before a
  Claude pass; a run that produces more than the cap is a bug in the
  prompt, not more work for Claude.

## Open questions / decisions log

1. **Priority field name.** SGLang documents the server-side flags but
   not the per-request field in the OpenAI-compatible body; confirm
   against the image's protocol before slice 4 (recommend: probe
   `/v1/chat/completions` with `priority` in the body and check
   `--abort-on-priority-when-disabled` behaviour on the running server).
2. **Agent budget 8 vs KV pool.** Recommend 8 at launch, read slice-1
   retractions, then set the number from the measured pool at 0.80.
3. **`apply_patch` tool shape.** Recommend an MCP tool that takes the
   OpenAI `apply_patch` v4a text and applies it in the tree (the prior the
   model has), rather than teaching it Edit; decide after slice 0 shows
   how often the undefined call happens through Claude Code's tool set.
4. **`reasoning_effort` default for the agent lane.** Recommend medium;
   high doubles wall time per turn with no evidence of a pass-rate gain on
   mechanical tasks (the harmony paper's 60.4 vs 53.3 is for SWE-bench,
   not lint).
5. **Speculative decoding.** Try EAGLE3 only after registries are
   reachable and in a window Reto deploys; default off.
6. **Tier W scope.** Recommend ruff/mypy residuals and doc sync first;
   test-from-spec stays R until slice 5's selection numbers exist.
7. **Who lands castor commits in a wave.** Recommend the coordinator (not
   the release owner) with `--checked`, in batches of ≤3 per authority
   v2, because the Claude read happens there anyway.

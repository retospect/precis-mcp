# Factory console + capability-reserved decentralized scheduling

> **Status: design-of-record, nothing built yet.** This is the agreed
> shape from a long design conversation (2026-07-17). It supersedes the
> ad-hoc `/env` inspector and folds together the operator console, the
> declarative pass/service registry, the single-plist collapse, the
> capability model, the resource-reservation scheduler, the quests
> fair-share view, the per-todo permission envelope, and the litellm
> retirement. Sliced at the end; each slice ships green independently.

## 0. The one-sentence principle

Everything the factory does — every worker pass, compute job, LLM call,
GPU relax — is **a consumer that runs where its capability lives,
reserves that capability locally, and releases it when done.** Hosts
publish what they *can* do and how much of it is free; work declares
what it *needs*; claiming a unit of work is the same transaction as
reserving the resource it consumes. There is no central scheduler and no
cross-node call for a local capability — work flows to the capability by
claim-gating, not by routing.

The console is the window onto that: one list per server of what runs
there, at what priority, on which model, with last-success / last-failure
/ raw-I/O, and a live switch — plus a quests tab that sets the strivings
whose priority flows down into the same work queue.

## 1. Why — the four things wrong today

1. **No single source of truth.** The worker "registry" is imperative
   `if _pass_enabled("x"):` blocks appending closures in `cli/worker.py`
   (~800 lines). Profiles are two `frozenset`s. Gates are scattered
   (`env_flag("PRECIS_*_ENABLED")` in worker.py, `_gate_enabled` inside
   pass modules, self-short-circuits). The `/env` page keeps a *fourth*
   parallel list — a hand-maintained 4-row `AgentSpec` tuple
   (`routes/env.py`) explicitly "kept loosely aligned" with the real call
   sites. Nothing maps a pass to its host, cost, prompt, or docs.
2. **Toggling a pass is a redeploy.** A gate is a plist `EnvironmentVariables`
   entry rendered from a host_var; flipping it is edit-host_var →
   re-render → `launchctl bootout`/`bootstrap`. There is no live switch.
3. **Routing is a mint-time pin, not a capability match.** Compute jobs
   carry `meta.params.target_node = "spark"`; the claim just matches the
   pin. A GPU node can starve on commodity work while a relax waits;
   there is no failover, no scarcity awareness, no visibility of an
   *unschedulable* job.
4. **Capability is conflated with provisioning.** `patent`/`edgar`/`python`
   show as "unavailable" not because a host physically can't do them but
   because a key/dep/env isn't wired — even though the key is already in
   the vault. Real physical scarcity (GPU) and incidental un-wiring are
   treated identically.

## 1a. Taxonomy of work — why there are two tabs, not one

Not everything is a todo. Work divides by *where the obligation comes
from*, and that source dictates the machinery — five shapes:

* **Grinders** — obligation *implied by data existing*: embed, summarize,
  chunk_keywords, classify, llm_summarize, corpus/paper_reconcile. Nobody
  decided "embed chunk N"; the chunk exists → it should be embedded.
  **Convergent** (the claim shape `WHERE embedding IS NULL` *is* the queue;
  caught up ⇒ idle), intentless, no rotation, no "done". Cost is orthogonal
  — a grinder can be expensive (llm_summarize burns budget); that is what
  its `prio`/trickle throttle is for.
* **Todos** — obligation from a *decision* (human or planner). These get
  the intent apparatus: rotation, priority, decomposition, blocking,
  ask-user, the failure-bubble, abandonment, a real *done*.
* **Quests** — the *striving* above todos: perpetual, never done; they
  don't do work, they *weight* it down the `serves` DAG.
* **Watches** — obligation from *time*: cron/`every:` spawners (casts,
  card_forge, briefing, reconcile cadence).
* **Daemons** — not work units: embedder, web, asa-bot, llama-swap.
  Standing servers others *call* — perpetual and **non-convergent**.

The dividing test for any new piece: *did someone decide this specific
unit, or is it implied by data?* (decided → todo; implied → grinder) and
*does it converge to idle, or serve forever?* (converge → grinder; serve →
daemon; complete-then-gone → todo). This is already in ADR 0044: the
**intent lane** (job owned by a todo — rotation + bubble) vs the **compute
lane** (job owned by an artifact — derived, idempotent, no rotation) *is*
the todo/grinder split at the job level.

The payoff: these shapes **diverge at the lifecycle layer but converge at
the resource layer.** A grinder's unit and a todo's minted job both become
work that reserves a capability (§5); the scheduler does not care which
shape produced it. So the console is **two faces over one engine** — a
*services* tab (grinders + daemons; `prio` = greediness) and a
*quests/todos* tab (intent; `prio` = weight) — with the reservation
scheduler beneath both. Forcing everything into todos would drown the one
substrate meant for *decisions* under a flood of *implications*.

## 2. The registry — one declarative table

New module `src/precis/workers/registry.py`: a frozen `ServiceSpec` row
per *thing that runs* (pass, job_type, compute service, standalone
daemon, LLM serving endpoint). Fields:

```
name, label, category            # ingest/discovery/acquisition/jobs/review/
                                 #   health/audio/compute/serving/daemon
kind         : pass|job|compute|daemon|serving
profile      : system|agent|standalone       # deploy topology, not a gate
requires     : set[cap-token]    # gpu, hermes-oauth, llm:<model>, cpu-heavy, mem_gb:N
uses_model   : bool              # → gets a model_pref control
uses_external: list[svc]         # anthropic|openrouter|unpaywall|s2|openalex|tts…
prompt_env   : str|None          # LLM passes only
cost_sources : list[str]         # llm_call_log source labels / service_calls keys
doc_skill    : str               # precis-*-help
one_line     : str
```

`cli/worker.py` reads gates/profiles from this table instead of scattered
literals; the `/env` `AgentSpec` tuple is deleted. A **totality test**
AST-parses `cli/worker.py` (mirroring the existing
`test_ref_pass_priority_keys_match_registered_passes`) so every
registered pass/job_type *must* have a spec — CI fails on drift. This
kills the four parallel lists and is worth doing regardless of the UI.

The catalog contents span five tiers (all in the one table, discriminated
by `kind`):

* **Standalone daemons** — embedder (`serve-embeddings`, per-host,
  `/healthz`+`/readyz`), web (uvicorn, melchior), asa-bot (Discord,
  hermes), `watch` (PDF ingest — the *paper ingestor*, inline `precis_add`).
* **System-worker passes** (every node) — embed, summarize,
  chunk_keywords, chase, fetch_oa, gp_fetch, tag_embeddings, auto_check,
  schedule, nursery, dispatch, sweeper, job_coordinator, wake_runner,
  job_ssh_node, clusterize, corpus_reconcile, paper_reconcile.
* **Agent-worker passes** (melchior/hermes/OAuth) — structural,
  deep_review, job_claude_inproc, quota_check, quest_dispatch, dream_agent.
* **Job types** — fix_gripe, plan_tick, reading_brief, meditation,
  card_forge, news_poll, briefing, cad/structure/diagram_propose,
  cad_discuss, good_search(+triage), draft_export, sandbox_run.
* **Compute services** (spark GPU, container-per-job via `ssh_node`) —
  struct_relax (GPAW/ML-potential), fold (AlphaFold3), retrosynth
  (AiZynth/ASKCOS+LinChemIn), TTS narration (cast_audio/briefing_audio,
  Kokoro), and the next plugin, catpath/pathway.

## 3. Capability — three classes, not a boolean

"Can this host do X" is a **conjunction of provisioning facts**, and the
facts fall into three classes with different dispositions:

* **PHYSICAL** — genuinely host-bound: GPU/CUDA (a Mac can't do it),
  a filesystem mount that exists on one host, an OS identity + OAuth
  (`hermes` + `~/.claude`). *Stays gated; tasks route on it.*
* **HEAVY-DEP** — gated because the dependency is large (torch, marker,
  GPAW, AlphaFold) and you don't want it in every venv. *Stays gated,
  co-moves with the physical need (heavy deps are the GPU-bound ones).*
* **INCIDENTAL** — gated *only* because a key (already in the vault via
  `get_secret`), a **light** dep (`epo_ops`, `yt-dlp`, `habanero`), an
  env string (a User-Agent), or a cache dir that any host could create,
  isn't wired. **Not real scarcity → universalize, drop the gate.**

The dividing test: *physical need OR heavy dep → gate; otherwise wire it
everywhere.* `patent` is the worked example — `PRECIS_PATENT_RAW_ROOT` is
just a cache dir, the EPO OPS creds are already in the vault, and
`python-epo-ops-client` is a light pip install. Three trivially-universal
things. Universalizing patent **dissolves** the todo-162488 failure
(drafting done anywhere, OPS sweep parks on "✋ needs you") — the sweep
just runs; there was never a real boundary. Same recipe for `edgar`,
`youtube`, and the data-source kinds. Concretely: default the cache dir,
pull creds from the vault, fold the light dep into the standard venv, and
**remove the `find_spec` hard-fail + `_REQUIRED_ENV`**. The MCP "Kinds
unavailable" list then shrinks to only the genuinely-physical — the
honest set.

Because the incidental class collapses, the routing **requirement
vocabulary shrinks to a handful of physical tokens** (`gpu`,
`hermes-oauth`, `llm:<model>`), and those are *derived* from a job_type's
already-declared `REQUIRES` (`EXECUTOR_PROVIDES` machinery) — not
hand-authored per todo. That is what makes "tasks declare their
requirements" tractable.

> **Skills follow-up.** Capability-goes-to-work naturally produces
> *smaller, capability-scoped todos* (the LLM-needing or GPU-needing
> sub-step splits off so it can route). The authoring skills
> (`precis-decomposition-help`, `precis-tasks-help`) should teach agents
> to author work that way — a small, single-capability unit routes; a
> monolithic one parks.

## 4. Trust is a write axis, orthogonal to capability

The worry about a small/less-trusted model on some host is not that it
can *read* a patent or *fetch* a transcript — skills are discoverable, so
even a small model can use a read kind. The worry is it *writes garbage*.
So the lever is **remove write, not remove the kind.** Read/fetch
capabilities go everywhere; **write/mutate is the trust boundary.**

This generalizes to a **per-todo permission envelope** — the dual of
capability. A todo declares its own least-privilege box, honored by the
executor regardless of host:

```
meta.envelope = { egress: none|api-only|open,
                  write:  none|scoped|full,
                  return: output-only }     # "just bring back the output"
```

Three enforcement tiers, chosen by how hard the guarantee must be:

* **Tool-level** (cheap, cooperative) — `claude_agent.py`'s
  `--disallowed-tools` / `--permission-mode` / `--bare`: drop the write
  and fetch verbs. Good enough for "don't touch the corpus."
* **Process-level** (real) — hand the task a **read-only DB role**
  (`agent_ro` vs `agent_rw`), so a write is refused by Postgres even if
  attempted.
* **Network-level** (hard) — the `claude_docker`/`sandbox_run` container
  with **no network namespace** (`--network none`); the only true egress
  denial. Tool-level "no fetch tool" is cooperative; a no-net container
  is enforced.

The envelope is the *permission* side of a work spec; the resource
`requires` (§5) is the *consumption* side. A no-write / no-egress task is
safe to run **anywhere** — which is what lets us universalize read kinds
without worrying about which host claims them.

## 5. The scheduling substrate — capability-reserved, decentralized

The claim path is already decentralized (`FOR UPDATE SKIP LOCKED`, pull,
no assigner). What changes: **declare requirements, not destinations**,
and **reserve the consumed resource in the same transaction as the claim.**

### 5.1 Resources: hard-counted vs soft-signalled

Hosts publish two kinds of resource, with opposite disciplines:

* **Hard-counted (correctness)** — `resource_slots(host, resource,
  capacity, free)`. LLM concurrency (`llm:qwen-27b → 4`), GPU
  (`gpu → 1`). Must be exact; over-subscription breaks things.
* **Soft-counted (optimistic)** — **memory**, modelled as a decrement,
  because a decrement is *predictive* (accounts committed RAM before it
  lands) where a measured-free threshold is only *reactive* (sees pressure
  after it's too late). Memory uses the **same expiring-lease rows as the
  hard slots** (§5.2) — the only difference is discipline: units are
  *estimates* (`mem_gb` hint), over-commit is *allowed* past the line, and
  the backstop is fail-and-retry, not refusal. **Every active thing holds
  a memory lease — including standing model-serving**: a resident
  qwen-27b in llama-swap holds a big *standing* lease for as long as it's
  loaded, so `free = total − Σ(all live memory leases)` stays honest (the
  model's RAM is never counted free). Measured memory-pressure (Linux
  `MemAvailable`; macOS `kern.memorystatus_vm_pressure_level` — *the same
  signal jetsam keys off*) is kept as a **veto**, not the primary gate:
  schedule against the decrement, but if measured pressure spikes anyway
  (unaccounted/non-precis RAM), back off regardless. `load1/5/15` stays a
  pure threshold (CPU has no clean per-consumer estimate).

So the resource layer is **one mechanism** — lease rows, `free = capacity
− Σ live units` — with a hard/soft flag deciding refuse-vs-allow. GPU,
LLM concurrency, and memory are the same table.

The disciplines differ because their failure modes do. A hard-slot race
means two jobs on a 1-GPU box — real breakage, so it must be exact. A
soft-signal misjudgment means an OOM / jetsam kill — which the **existing
recovery already absorbs**: the lease sweeper reclaims the crashed job
(`lease_until` + `PRECIS_STUCK_JOB_HOURS`) and it re-queues. *You don't
need memory accurate because retry catches every miss* ("we can always
fail and try again"). Trying to account RAM exactly would be false
precision — the OS shares it, caches move, other processes churn it.

### 5.2 Reserve-at-claim

A job declares `requires: { llm:qwen-27b: 1 }` or `{ gpu: 1 }` (+ optional
soft hints `mem_gb`, `cpu-heavy`). Claiming is one transaction:

```sql
-- hard reserve: the conditional decrement IS the lock (no race)
UPDATE resource_slots SET free = free - 1
 WHERE host = :me AND resource = 'llm:qwen-27b' AND free >= 1
 RETURNING host;                 -- zero rows → capability extinguished → skip job
-- …then, only if reserved, claim the job row FOR UPDATE SKIP LOCKED,
--    in the SAME transaction, after checking soft signals (load/mem) loosely.
```

Release = `free = free + 1` on job terminal; a crashed holder is reclaimed
by the same lease-expiry sweeper that already frees stuck jobs. So
`resource_slots` **is** the fleet-wide concurrency view — first-class, not
a bolt-on redis counter. Multi-resource jobs reserve their whole set
**all-or-nothing** in the one transaction (avoids partial-reservation
deadlock). A soft-gated job that fits nowhere waits and retries, bounded
by a **retry cap** (same shape as the plan-tick resume-streak cap) so it
bubbles "needs a bigger host" instead of spinning.

### 5.3 Claim ordering — scarcity, priority, age

Within the claimable set a worker orders:

```
ORDER BY capability_rarity DESC,   -- fewest hosts that could do this → first
         effective_prio    DESC,   -- refs.prio, flows down the quest/todo DAG
         age               DESC    -- oldest wins; the anti-starvation term
```

`capability_rarity` = how few hosts provide the required capability
(computed from the same `resource_slots`/capability matrix the console
renders — one table, two uses). So a GPU node services GPU work before
commodity `embed` (which any node can do), the rare lane never starves
behind commodity work, and age keeps commodity work from starving
forever. On a node without the capability the rare job isn't even a
candidate, so it just drains commodity work.

### 5.4 Consequences

* **`target_node` pinning is retired.** `struct_relax`/`fold` become
  `requires: { gpu: 1 }` — which auto-serializes GPU work (capacity 1)
  *and* lands wherever a GPU slot is free, no central pin, with failover
  the day a second GPU node exists. `target_node` survives only as an
  optional cache-affinity hint.
* **An unschedulable job is first-class.** A job whose `requires` no
  enabled host provides raises a visible alert (the console's
  "unschedulable" state) instead of silently parking.
* **The console's capability matrix and the scheduler read the same
  table** — declaring "melchior serves qwen-27b, cap 4" both renders the
  row and seeds the reservation counter.

### 5.5 How the capability map is populated — self-probe

Each host discovers what it can do and writes a per-host capability+slots
row; the `heartbeat` pass is the home (it already runs per-node every 60s
and UPSERTs `host_heartbeat`). Capability has two halves on two cadences:

* **Presence** (changes rarely — boot + every few minutes): light,
  in-process probes — `importlib.util.find_spec("epo_ops"|"gpaw"|…)` (the
  same check `dispatch.py` does at fail-time, harvested up front),
  `shutil.which("podman")`, GPU via `nvidia-smi`/CDI, `get_secret(...)`
  reachability, mount/dir existence, OAuth validity. **Probe for
  *presence*, not *correctness*** — the cheapest launchable signal
  (`--version`, `podman image exists`), never a full exercise.
* **Liveness / slots** (volatile — every heartbeat): does local llama-swap
  answer `/v1/models` and *which models is it serving right now* (it
  hot-swaps, so served-models is real discovery), embedder `/readyz`,
  current load/memory, live free-slot counts.

Three sources converge into the map: **declared** (ansible provisioned it +
topology intent), **probed** (present + live), and **observed** (real work
succeeds). The **observed** loop closes it: a capability that probes present
but keeps *failing real work* **auto-retracts** (repeated `last-failure`
demotes the host's advertised capability until it recovers) — the same
last-success/last-failure the console shows, fed back into routing.
**Declared-vs-probed drift is a first-class health alert** ("declared for
fold, but the AF3 image probe fails" — the shape `llm_reconcile` already
uses for proxy-drift). Capacity is *declared* (`served_by.max_parallel` — a
concurrency choice); liveness is *probed*; effective slots = declared
capacity **gated by** probed liveness (model not loaded ⇒ 0 slots).
Staleness degrades to fail-and-retry, so the map need not be perfectly fresh.

## 6. LLM serving — routing dissolves into the scheduler

litellm today is not a thin proxy: it is a **multi-node inference control
plane** — pooling one model across melchior+balthazar, fronting ollama /
llama-swap over SSH tunnels, keeping models warm, with its own budget DB.
Under §5 that entire *routing* role **ceases to exist**, because a
consumer runs where the capability lives and calls **localhost** — nothing
ever HTTP-calls another node for local inference.

* **Job-shaped LLM work** (plan_tick, quest_tick, casts, reviewers)
  routes to a host serving the model with a free slot and calls its own
  localhost llama-swap.
* **Inline LLM calls inside a pass** (chase-verify, judges, llm_summarize)
  do the same by making the *pass* capability-gated: it claims/runs only
  on a host serving the needed model, reserves a **local** slot, calls
  localhost, releases. A host without the model simply doesn't run the
  pass, so work flows there by claim-gating. Reservation target is always
  `(me, resource)` — no distributed reservation, no reserve-then-remote.

So litellm's three jobs redistribute: **aliasing → `llm` catalog cards**,
**load-balancing → claim-gated local reservation (no balancer)**,
**budget DB → `llm_call_log`** (richer already). **llama-swap stays** (the
per-node VRAM model-swapper — a different job). litellm-the-proxy retires.

**Serving is declared on the `llm` card**, and that one fact is the whole
setup. A card's offering grows `served_by`:

```
llm card "qwen3.6-27b"
  offering: transport = openai_compat (llama-swap)
    served_by:
      - { host: melchior,  endpoint: :<port>/v1, max_parallel: 4 }
      - { host: balthazar, endpoint: :<port>/v1, max_parallel: 2 }
```

`max_parallel` **is** the `resource_slots` capacity. The console renders
it model-first (the card) and host-first (melchior's list shows "serving
qwen3.6-27b, cap 4" beside its passes). Distribution is a provisioning
dial: to add inline-LLM throughput, add a `served_by` row — the
capability-gated passes start claiming there too. This is the
**slurm-ollama** shape later (schedule onto a node with a free slot).

**Third-party services** (OpenRouter, `claude -p`, Anthropic) are external
interfaces with keys in the DB — **any host, no reservation**. The one
finite shared third-party thing is the Anthropic **OAuth quota**
(`quota_check` snapshot), handled by the budget breaker as fleet-budget,
not per-call slots.

**Model selectability.** `PRECIS_LLM_BACKEND` is today a single coarse
global switch (anthropic ↔ oss). The console's per-pass `model_pref`
(populated from `kind='llm'` cards) is the fine-grained replacement:
each model-using row picks a card, the card carries its own transport, so
selecting a model *is* choosing the backend, per pass. The global env
flag becomes the fallback default. Non-model passes (chunk_keywords, the
SQL passes) get no dropdown.

## 7. The single-plist collapse

Spark already proves the target: one `precis-worker.service` loads every
compute capability, each env-gated by a systemd drop-in. Swap those gates
(and the macOS host_var→plist gates) for a **DB config the worker
consults live**, and the edit→re-render→bootout cycle disappears for the
gate-flag subset.

`service_config(host, service, prio, model_pref, write_level, updated_at,
actor)` where **`prio` is both the switch and the scheduling weight**:
`0 = do not run`, `1..N = run at this claim weight` (§5.3). A resolver
consults the DB row, falling back to the env/profile default. The worker
picks up a flip on its next cycle — seconds, no redeploy. Capability is a
separate fact (from provisioning); an unsupported cell renders **grayed,
prio locked at 0, with the reason** ("missing epo_ops dep · no EPO creds ·
no raw root" — distinguishing *installable-from-vault* from
*unprovisionable-here*).

**The hermes/OAuth boundary is dissolved by moving the token to the
vault.** hermes exists only as the UNIX principal owning the Claude
Max-plan OAuth (`~/.claude`), under which worker-agent / dream / asa-bot
run so they can make `claude -p` calls. **Decision: migrate that
long-lived token into the secrets vault beside the other keys**, and wrap
it — extend the existing `ensure_oauth_token` into a *materializer* that
pulls the token from the vault at call time, presents it to the CLI
(credentials file / `CLAUDE_CODE_OAUTH_TOKEN`), runs, and cleans up. One
host owns the *refresh* (writes the new token back to the vault);
everyone else reads. Then **hermes retires** — worker-agent, dream, and
asa-bot all run as `deploy`, and the single-plist collapse goes clean
(agentic work runs on any vault-reading host, no longer melchior-pinned
by identity). Two riders: the OAuth **quota** is still one shared account
cap, modelled as a **fleet-wide** resource (the `quota_check` snapshot +
budget breaker already track it) — "any host can call; the account has
one budget"; and the Max-plan token is higher-value than the EPO/S2 keys
(it is *spend*), so scope vault **read** access on that secret to the
roles that need it.

**Three things stay separate daemons** — different *processes*, not gate
flags a DB can flip: **embedder** (a model server the worker calls; must
be healthy before the worker boots), **web** (uvicorn), and **asa-bot**
(Discord bridge — our only Discord interface; stays, but as `deploy` once
the token is vaulted). These appear in the console as rows (health,
last-error, raw-I/O) but their control is start/stop, not `prio`.

The thin timers (dream, cron-tick, watch-poll, reconcile, anki-sync) are
one-shot invocations of the same binary and fold into the worker's own
scheduler (`precis cron tick` already exists) — except `reconcile`
(deliberately single-host on caspar) and `dream` (needs hermes).

## 7a. What retires

The redesign is net-subtractive. Clean retirements:

* **litellm** (fully) — routing dissolves (§6), aliasing → `llm` cards,
  budget → `llm_call_log`. **llama-swap stays** (per-node VRAM swap).
* **hermes** as a special principal (slice 0, §7) — token → vault;
  worker-agent/dream/asa-bot run as `deploy`.
* **The thin timer daemons** — dream, cron-tick, watch-poll, anki-sync
  fold into the worker's own scheduler (`reconcile` stays caspar-pinned).
* **The four parallel lists** — worker.py `if`-blocks, the two profile
  `frozenset`s, and the `/env` `AgentSpec` tuple → one `ServiceSpec`
  registry.
* **`target_node` pins** → `requires:` capability tokens (§5), except the
  try-button "run it here" override.
* **The `PRECIS_*_ENABLED` plist gate flags** → `service_config.prio`,
  live-toggled from the console.

Stays, and *why* it can't be a DB flag: **embedder** (separate model-server
process), **web** (uvicorn), **asa-bot** (Discord bridge — our only one),
**llama-swap** (VRAM swapper), and the physical/heavy-dep capabilities
(GPU, container images, mounts — *provisioned* by ansible, only *gated* by
the DB).

## 8. The console

`/factory` (absorbs `/env`). **A host strip** at top: per machine, load
1/5/15 + memory pressure + temp + `PRECIS_LOAD_CEILING` state +
worker-alive/dead (heartbeat + `worker: started` boot rows + the nursery
dead-worker signal). Then **one list per server**, sortable by server or
by capability. Each row (a service):

| column | source |
|---|---|
| service (grayed + reason if unsupported) | registry × capability matrix |
| meta (GPU?, model dropdown, external-paid vs local) | registry; `llm` cards |
| **prio** (0 = off; editable; locked when unsupported) | `service_config` |
| last success (relative "3 min ago") | last `BatchResult` ok>0 / job `STATUS:succeeded` / `llm_call_log.errored=false` |
| last failure (relative) | last `worker_logs level=ERROR` / job `STATUS:failed` / `llm_call_log.errored=true` |
| **test** button | embedder `/readyz`; a pass = "run once" (`--only X --once`); daemon = heartbeat freshness; compute = canary job |
| **raw I/O** button | `llm_call_log`+`llm_blob` (LLM) / `job_event`+`job_result`+staged files (compute) / BatchResult payload (pass) |

The **test** button is the *presence* check; a **try** button is the
*correctness* one — it mints a tiny **canary job pinned to a chosen host**
(`requires: {host: spark}`, so only that box can claim it) and runs the
cheapest real exercise (a toy relax, a 1-token fold stub). This is where
host-pinning legitimately survives the move to requirements-routing — the
manual "run it *here*" override — and it doubles as how a freshly-
provisioned capability earns routing trust: a green try feeds the
observed-health loop (§5.5) and normal routing starts using the host.

A **cost/volume panel**: per-pass and per-service spend from
`llm_call_log` by `source` (dollars where we have them) + **call counters**
for the un-costed external touchpoints — a `service_calls(day, host,
pass, service, calls, units, unit_kind)` rollup wired into fetch_oa /
S2 / OpenAlex / EPO / TTS (TTS in render-minutes), so the page shows
*volume* even where there is no dollar cost — plus the budget-breaker
state.

## 9. The quests tab

Same mental model as services — *set a priority, the system allocates
proportionally* — on the other substrate (which strivings get attention).
The `serves` DAG is already walked by `view='tree'`; the tab is that view
plus inline controls:

* **enable/disable** → the existing lifecycle `active ↔ dormant`
  (`abandoned` = hard off); only active quests pull weight today.
* **priority** → `refs.prio` (1..10) via the `PRIO:` tag, already
  flowing down the DAG (max-agg, `STRIVING_DECAY` per hop).

The **fair-share-over-a-rolling-window** policy the user wants is
**already implemented**: `allocator.over_budget`/`weekly_spend` meter each
active quest's spend against its priority-proportional share of a rolling
budget window. The work is *not* a rewrite:

1. **Parameterize the window** from 7 days to 24/48h (a `days` arg + env).
2. **Close the cost-attribution gap** — `quest_tick`'s `llm_call_log`
   rows land with null `ref_id`+`cost`, so per-quest window spend is
   currently under-counted; the fair-share meter needs real numbers.
   (This is *also* the tab's cost column.)
3. **Surface + tune** in the tab: the prio slider + a share-consumed vs
   share-allotted bar over the window.

The weighted round-robin (`e1d3fec4`: progress-decay anti-spin + aging
floor + cool-to-dormant) is *compatible* — it orders *which quest ticks
next* (short-term), while `over_budget` bounds *total draw over the
window* (proportional share). Together they already deliver the ask; the
tab exposes and tunes them.

**The two tabs are one priority cascade, not two pages.** Quest priority
already flows down into the work layer (reweight sinks: rotation,
acquisition, reading), so a high-prio quest's compute jobs inherit higher
claim `prio` in §5.3. Quests (top) → services (bottom), one `prio` field
connecting them.

## 10. Slice plan

Each ships green independently; 1–4 stand up the console + switches +
model choice without touching the scheduler or daemons.

0. **hermes OAuth → vault.** Move the Max-plan token into the vault + a
   `ensure_oauth_token` materializer; run worker-agent/dream/asa-bot as
   `deploy`. Independent of the console; unblocks the collapse (slice 10)
   and de-pins agentic work from melchior. Do early — small, and it
   removes the one hard boundary everything else assumes away.
1. **Registry + totality test.** `ServiceSpec` table; rewire
   `cli/worker.py` to read it; delete the `AgentSpec` tuple. Pure
   refactor, no behavior change. Keystone (kills four parallel lists).
2. **`service_config` + prio-gate resolver.** DB row consulted live,
   falling back to env/profile default; `prio 0 = skip`. Provable by CLI
   (`precis service prio <host> <name> <n>`) before any UI.
3. **Read-only `/factory` console.** Host strip + list-per-server + last
   success/failure (relative) + test + raw-I/O; absorbs the env detail
   view. LLM cost from `llm_call_log`.
4. **Live prio switch + model picker.** Write `service_config` from the
   page; `model_pref` dropdown wired through the `llm` catalog (auto or
   pinned).
5. **Capability universalization.** Drop the incidental gates
   (patent/edgar/youtube/data-sources): default cache dirs, vault creds,
   fold light deps into the standard venv, remove `find_spec` hard-fails.
   Shrinks the "unavailable" set to physical-only.
6. **Resource substrate.** `resource_slots` + soft signals in heartbeat
   (add memory pressure); reserve-at-claim; scarcity+prio+age ordering;
   unschedulable alert. Jobs declare `requires`; `target_node` → hint.
7. **litellm retirement.** `served_by` on `llm` cards seeds
   `resource_slots`; capability-gated inline-LLM passes call localhost;
   the router pooling provider (only if any remote call survives);
   decommission the litellm daemon; llama-swap stays.
8. **Per-todo envelope.** `meta.envelope` honored by the executor across
   the three enforcement tiers (disallowed-tools / read-only DB role /
   no-net sandbox).
9. **Quests tab.** Parameterize the fair-share window; close the
   quest-cost attribution gap; render the DAG + prio + share bar.
10. **Single-plist collapse.** Fold worker variants + thin timers into
    one DB-controlled daemon per host (resolve the hermes/OAuth run-as
    question first). embedder/web/asa-bot stay separate.
11. **(Later) Model comparison / eval.** Run a task through model A vs B,
    score the outcome — the golden-task harness already stubbed in the
    `llm` catalog (`record_eval` is its write surface), surfaced on the
    model picker as "compare."
12. **Fleet-repo consolidation (§14).** Fold `~/work/cluster` into the
    monorepo as an excluded `deploy/` tree; real inventory + vault become a
    private overlay; deploy installs from the tree, retiring the
    `*_git_ref` indirection. Enables slice 10 (the plists it renders now
    live beside the code) and shares slice 0's `populate-secrets-vault`
    playbook. Independent of 6–9; do when the cross-repo drift bites.

## 11. Decisions taken / remaining

Resolved in conversation:
* **hermes → vault** (was: run-as question). Migrate the Max-plan OAuth
  token into the vault + a materializer wrapper; hermes retires; the
  collapse goes clean (§7). New slice **0** (it unblocks slice 10).
* **memory = soft-counted decrement** (was: threshold vs reserve). Decrement
  a per-host memory lease for *every* active consumer including standing
  model-serving; measured pressure is a veto (§5.1).
* **global concurrency correctness** is handled by `resource_slots`, so the
  distributed-router caveat is moot.

Remaining:
* **cost of retiring the Max-plan flat-rate** — vaulting the token is free,
  but if agentic load later moves off the Max OAuth onto metered API keys,
  that is a spend decision (mitigated by pushing reasoning to local qwen).
* **vault read-scope** for the high-value OAuth secret (which roles/hosts).

## 12. Implementation status (2026-07-17)

Built + green in-container on `worktree-zany-puzzling-thompson` (not yet
shipped/deployed):

* **Slice 1** — `src/precis/workers/registry.py` (`ServiceSpec` table);
  `cli/worker.py` derives profiles + gates from it; `routes/env.py`
  `AgentSpec` deleted; AST totality test. Pure refactor (snapshot-proven).
* **Slice 2** — migration `0072_service_config`, `workers/service_config.py`
  (`ServiceConfigResolver`), a per-cycle `run_loop` `pass_gate`, and the
  `precis service prio|model|clear|list` CLI.
* **Slice 3** — read-only `/factory` (`routes/factory.py`): host strip +
  per-category service list + live prio + last-ok/last-fail from
  `worker_logs` (via `ServiceSpec.log_handler`).
* **Slice 4** — `/factory` write side: host selector + editable prio +
  model_pref dropdown (from the `llm` catalog), POST → `service_config`.
* **Slice 5** — incidental patent/edgar gates dropped from
  `requires_env` and defaulted in `precis.config`
  (`cache_root`/`patent_raw_root`/`edgar_raw_root`/`edgar_user_agent`);
  edgar available everywhere, patent gates only on the EPO creds (vault).
* **Slice 0 (code)** — the OAuth materializer already sources the token
  from the vault (`utils/claude_oauth.ensure_oauth_token`); asa's mirror
  (`asa_bot/oauth.py`) gains the same vault leg over its existing
  `PRECIS_DATABASE_URL`, so agentic daemons can run as `deploy` with no
  `~/.claude` state.

### Slice 0 fleet cutover — the ops sequence (deploy-time, do in order)

The code ships safe (vault is a *fallback*, so nothing changes until the
token is vaulted and the run-as is flipped). The live migration is an
**ordered** ops sequence — do NOT flip run-as before the token is vaulted,
or every agentic call 401s:

1. **Seed the vault** on one host with DB access:
   `printf '%s' "$TOKEN" | precis secret set CLAUDE_CODE_OAUTH_TOKEN`
   (the token is `claude setup-token`'s long-lived value, today in
   `~hermes/.claude_oauth_token`).
2. **Verify** every agent host can read it: `precis secret get
   CLAUDE_CODE_OAUTH_TOKEN` reveals it, and a `claude -p` smoke on that
   host authenticates via the vault leg (temporarily rename the file).
3. **Flip run-as → `deploy`** for `com.precis.worker-agent`,
   `com.precis.dream`, and asa-bot in the cluster plists/playbooks, and
   ensure `PRECIS_DATABASE_URL` is in their env (asa already has it).
4. **Scope vault read** on `CLAUDE_CODE_OAUTH_TOKEN` to the roles that
   need it (it is *spend*, higher-value than the EPO/S2 keys).
5. **Retire hermes** as a principal once green; the single-plist collapse
   (slice 10) then has no identity boundary left to honour.

`quota_check` still tracks the one shared OAuth **quota** as fleet-budget
— "any host can call; the account has one budget" (§7).

## 13. `claude -p` in a container — the vaulted token makes it host-free (note)

Slice 0 already removes the one thing that pinned `claude -p` to a host:
its identity was `~/.claude` **filesystem** state owned by hermes. Once the
`ensure_oauth_token` materializer sources `CLAUDE_CODE_OAUTH_TOKEN` from the
vault (or an `ANTHROPIC_API_KEY` for metered calls), the invocation is
**stateless** — its only inputs are an env var, the prompt, and a reachable
MCP server. That is exactly the precondition for running it in a throwaway
container:

```
docker run --rm \
  -e CLAUDE_CODE_OAUTH_TOKEN="$(precis secret get …)"   # or ANTHROPIC_API_KEY
  -e PRECIS_DATABASE_URL=…                                # DB role = envelope.write tier
  --network <none|api-only|open>                          # = envelope.egress tier
  precis-agent:latest  claude -p "…"
```

No mounts, no home state, no principal. This is not a *new* tier — §4
already names the `claude_docker` / `sandbox_run` container as the
network-level enforcement tier. The observation is that **the vault token
lets the container become the *default* agentic executor, not just the
hard-isolation special case.** The §8 per-todo envelope then simply
parameterizes the *same* `docker run`: `egress` → the `--network` choice
(`none` / slirp-allowlist to `api.anthropic.com` + our pgbouncer / open),
`write` → which DB role env is injected (`agent_ro` vs `agent_rw`), `return`
→ output-only capture. One execution primitive, three knobs.

What it buys beyond isolation: (a) **real egress control** — `--network
none` is enforced, not the cooperative "no fetch tool"; (b) **no `~/.claude`
cross-talk** between concurrent agent jobs on one host (each container is a
fresh HOME); (c) a **reproducible toolchain** — the image pins the `claude`
CLI + MCP server + skill set, so a job's behavior doesn't drift with the
host's installed version; (d) **resource reservation collapses onto
container concurrency** — a §5 `resource_slot` for the agent capability *is*
the cap on concurrent containers, and the lease sweeper reaps a crashed
container the same way it reaps a dropped slot.

**What calls what** (the actual chain, so the transport question answers
itself):

```
precis worker (agent profile) · claude_agent.py
  └─ shells out:  claude -p "<prompt>"  --mcp-config <precis stdio>
       └─ the claude CLI spawns:  precis serve   (stdio child, ONE per call)
            └─ precis serve → DB  (PRECIS_DATABASE_URL)
```

**(1) MCP transport — resolved: bake it in, stdio, no daemon.** In the
`claude -p` path `precis serve` is *not* a long-lived server — it is a
per-invocation stdio subprocess the CLI spawns and reaps with the prompt.
"How stable if baked in?" is the wrong axis: there is no uptime to keep —
if it dies, that one call fails and the job retries via the lease sweeper.
It needs no restart machinery because in this mode it is not a service. And
it is the *same wheel* the worker already runs, so baking it in adds no new
stability surface. The "dial a host socket" alternative is strictly worse
here: it re-introduces a host dependency *and* a long-lived daemon you
genuinely would have to keep alive. (The interactive `precis serve` that
Claude Code / a human talks to is a different, long-lived consumer and is
unaffected.) So: **bake `precis` into the image; `claude -p` spawns
`precis serve` over an in-container stdio pipe; zero network for the MCP
link.**

**(2) OAuth vs API — resolved: one container, two entrypoints.** These are
two execution *modes*, and the container serves both with the same envelope
knobs — only the entrypoint and the injected secret differ:
* **OAuth (Max-plan token, ~90% cheaper — the reason `claude -p` exists at
  all):** the container runs `claude -p`, which spawns `precis serve` stdio.
  This is the **default, volume** path. Token materialized from the vault
  (slice 0). The Max quota is one shared account cap (§7 fleet-budget), so
  scope its vault read tightly.
* **API key (metered — the reason the container must *not* hard-code
  `claude -p`):** skip the CLI entirely, call the Anthropic API directly
  (SDK tool-runner) with the precis MCP tools wired in-process. No
  `claude -p`, no stdio `precis serve`. The **escape hatch** for jobs that
  need higher rate limits, a pinned model, or when the shared Max quota is
  spent.
Mode is chosen per-job by cost policy; the image, the envelope, and the DB
leg are identical. That is a unification, not two designs.

**(3) DB reachability — `PRECIS_DATABASE_URL` is the one true config.**
Agreed and load-bearing. It means `--network none` is honest only for the
rare *pure-compute* sandbox job (no DB, no API, output-only). Every agentic
job needs the DB, so its real egress tier is **`api-only` with a two-entry
allowlist** — `api.anthropic.com` (or the local model endpoint) + the
pgbouncer `host:port` — *not* `none`. One injected config
(`PRECIS_DATABASE_URL`), riding the allowlist; skills and kinds ride the DB
or the image.

**(4) Image/pull cost — yes, and here is the precise version so it isn't
hand-wavy:** *not* a per-job build. **One host-resident `precis-agent`
image, digest-pinned**, layered on the existing dev base, holding the same
wheel the worker already installs + the `claude` CLI + the skill set,
rebuilt on the same cadence as the wheel/CLI bump. `docker run --rm` against
a resident image is milliseconds; the "pull cost" amortizes to ≈0 because it
is the wheel you already ship, frozen into a layer.

The one genuinely-open item is **rollout ordering**: containerize the agent
executor *after* slice 0 (token in vault) and alongside slice 8 (the
envelope tiers it enforces), so we never run a container that can't auth.

## 14. Fleet-repo consolidation — the cluster IS precis's deploy substrate

The `~/work/cluster` Ansible repo has no purpose but running precis: asa
merged in and hermes retires (no second tenant); the compute stacks
(aizynth / alphafold / dft / tts / llamacpp / ollama / litellm) exist only
to feed precis; postgres / redis / pgbouncer / backups have precis as sole
consumer; only tailscale + NFS are genuinely "these are machines," and even
those are arguable. So the earlier "don't merge, it's half other stuff"
objection is withdrawn: **the deploy recipe should live with the thing it
deploys.**

**The pivotal constraint: `retospect/precis-mcp` is PUBLIC.** So the merge
is a *layered* fold, not a dump:

* **Public layer → `deploy/` in the monorepo.** Portable, secret-free: all
  of `roles/`, `playbooks/`, `site.yml`, `bootstrap-*.yml`, `ansible.cfg`,
  callback plugins. Role `defaults/` reference `{{ vault_* }}` vars — they
  carry no literal secrets, so they are safe to publish once verified.
  Excluded from the wheel/sdist (both are allowlists — wheel ships only
  `src/…` packages, sdist `include` is `src/ tests/ docker/ docs/ scripts/`
  — so `deploy/` is out of both by default; "pip package that contains
  Ansible" is a non-conflict, no active exclusion needed).
* **Sample files are load-bearing, and they live here (public).** The
  overlay being private *sharpens* their job: they are the **contract**
  between the public roles and the private overlay, enumerating what the
  overlay must supply. Ship a `.example` per overlay file: `hosts.yml`,
  `group_vars/all/{vault,topology,precis_env,main}`, `host_vars/<host>`.
  Rules that keep them useful rather than rot-bait: (a) **`vault.yml.example`
  is a secrets *manifest*** — every `vault_*` key the roles reference, with
  placeholder values, authored minimal (never a blanked copy of the real
  file, which would leak structure + hostnames); the cluster repo already
  has `inventory/group_vars/vault.yml.example`, extend that pattern.
  (b) **Lint the contract**: CI greps `vault_[a-z_]*` across `deploy/roles/`
  and fails if a referenced secret is missing from the example — so a new
  `{{ vault_* }}` can't ship undocumented. (c) `hosts.yml.example` /
  `topology.yml.example` use **placeholder topology** (`node-a`, `100.x.x.x`,
  a fake tailnet) — real IPs never appear in the public tree.
* **Private overlay → a separate private git repo, NOT in the public repo.**
  Exactly the site facts: `inventory/hosts.yml` (Tailscale IPs `100.x`, LAN
  IPs, tailnet `aidev`, host→role map), `inventory/group_vars/all/{vault,
  topology,precis_env,main}.yml`, `inventory/group_vars/precis_anki_sync/
  vault.yml`, `inventory/host_vars/*.yml` (per-host numeric tuning), and
  `.vault-pass`. **Decision: these live in a private git repo cloned into
  `deploy/inventory/`** (gitignored in the monorepo) — versioned and
  portable to a new cluster, not a hand-maintained local dir. `ansible.cfg`
  keeps `inventory = inventory/hosts.yml` relative to `deploy/`. New
  cluster = clone monorepo + clone overlay into `deploy/inventory/` + run.

**The upgrade, not just a move — install-from-tree.** Today
`roles/precis_web`, `roles/mcps`, `roles/precis_worker` install
`precis-mcp[...] @ git+https://github.com/retospect/precis-mcp@{{
precis_web_git_ref }}` — app code and its deploy config live in two repos
and chase a branch HEAD. In the monorepo, deploy installs the **checked-out
tree** (build a wheel in a deploy pre-step, or `uv pip install <repo
path>[extras]`), so a single commit changes a worker's behavior and its
daemon/ansible config **atomically**, and `precis_web_git_ref` /
`precis_worker_git_ref` disappear. (Genuinely-external installs —
`catpath`, `remarkable-mcp`, `sortie-mcp` — stay `git+https`.)

**Two things that decide clean-vs-mess** (both real work, not mechanical):
1. **Path-scoped CI.** App PRs run pytest / ruff / mypy; `deploy/**` PRs run
   ansible-lint / yamllint. GitHub Actions path filters — mixed cadence is
   fine only if CI is filtered.
2. **A crisp overlay boundary.** `group_vars/all/{main,precis_env}.yml` and
   `host_vars/*` today mix true site config (IPs, PG tuning numbers, the
   capability→node map) with portable role behavior. The refactor's actual
   work is drawing the line **per line**: overlay = inventory + secrets +
   per-host numeric knobs + `topology.yml`; everything else → portable
   `roles/*/defaults`. Get it wrong and "portable" is fiction.

**Trim, then merge.** The cluster repo carries dead scaffolding
(`tmp_*.sh`, `tmp_*.sql`, one-shot `run-*.yml` migrations) that should be
dropped, not published. Consolidation order: (a) trim the temporaries;
(b) triage the overlay boundary, emitting `.example` files; (c) move the
portable tree to `deploy/`; (d) wire install-from-tree + path-scoped CI;
(e) stand up the private overlay repo/dir + gitignore. The cluster repo is
local-only (no remote), so no upstream history is stranded.

This is **slice 12** — independent of the scheduler slices (6–9), but it
*enables* slice 10 (the single-plist daemon's plist template lives beside
the worker code it launches) and it already ships the slice-0 tool
(`playbooks/populate-secrets-vault.yml`).

**Where it lands in the slice plan:** additive, and it *tightens* rather than
changes 6/7 — it's the natural build-out of **slice 8** (the envelope's
network tier stops being a special sandbox and becomes the normal executor),
gated on **slice 0** (token in the vault — code done, cutover pending §12).
It does not touch the registry (§2), `service_config` (§7), or the resource
substrate (§5) except to give the agent slot a concrete unit (one slot = one
container).

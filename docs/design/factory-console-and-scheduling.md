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

### 4a. Trust also gates spend — money and premium compute

Write/egress is only half the trust story. The other half is **cost**: some
work spends **real money** (metered LLM APIs, OpenRouter, paid data tiers) or
ties up **scarce/expensive compute** (a 10-minute GPU `fold`, a DFT relaxation,
supercompute). Those are consumables too — but *authorizing* them is a trust
question, not a capability one. The distinction stays crisp:

* **Capability** = *can* it run here (spark **has** a GPU; any host **can** call
  OpenRouter) — discovered.
* **Trust** = is this work **authorized to spend** the expensive thing (may a
  speculative quest tick burn a 10-min fold or a $5 OpenRouter call?) — declared.

So the envelope grows two cost facets alongside the affect/exfil ones:

```
meta.envelope = { egress, write, return,          # what it may affect/exfil
                  spend:   none | metered | full,  # for-pay APIs / real $   (NEW)
                  compute: local | premium }       # GPU / supercompute      (NEW)
```

Each facet is enforced at its **own** chokepoint (the executor already owns the
first three):

* **spend → the LLM router + the budget breaker.** A call to a metered
  model/API is refused unless `spend ≥ metered` **and** the fleet breaker has
  budget left — two gates, both must pass (per-todo *authorization* × fleet
  *availability*). §13's two entrypoints **are** the spend tiers: OAuth Max-plan
  (≈free, the default), metered API key (real $, the escalation), local OSS
  (free). Choosing a spend tier *is* choosing a transport.
* **compute → the claim path (§5 reserve-at-claim).** Reserving a `gpu`/premium
  slot requires `compute ≥ premium`, so capability (slot free) **and**
  authorization (envelope permits) are both needed. An idle GPU does not mean a
  backlog-groom todo may fire a fold on it.

**The safety default inverts here.** Write/egress can default *permissive* dark
(today everything already writes), but spend/compute should end **deny-by-default
— authorize expensive consumption up, never restrict it down** — the same
instinct as "a surprise-discovered capability defaults to prio 0" (§15f). Ship
permissive-dark to avoid a behavior change, assign spend/compute tiers to the
job types that legitimately need them, then flip the default to deny once the
legit spenders are authorized. The quests fair-share allocator (§9) is the
existing spend-governance mechanism for one work-source; the envelope's
`spend`/`compute` tiers are the per-todo generalization across all of them.

This is a follow-up facet of slice 8's envelope (call it **8c**), landing on the
same `docker run` knobs §13 already parameterizes — spend picks the injected
secret (OAuth token vs API key vs none), compute picks whether the container is
allowed to reserve the GPU slot.

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

**Each node has a capability set; external LLMs are in *every* node's set.**
The primary representation is node-centric (matching the config `host→services`,
the per-node probe, and the `(host, resource)` tables): every node carries a set
of capabilities. A capability's "placement" is just the **inverse index** — the
nodes whose set contains it — derived for "where can X run" queries, not the
primary fact. In that view **external LLMs are default members of every node's
set** (the key is vault-wide, reachability is the only precondition), so they
need *no* line in any host's `services` — they're implicit everywhere. A
**local** model, by contrast, is in only the `served_by` hosts' sets. This is
why external-LLM work claims on any node, gated not by which set it's in but by
**egress-trust** (§4: `egress: none` can't reach it; `api-only` with the
anthropic allowlist can) and **budget** (§4a / the breaker). Membership
universal; availability = egress × budget, not host.

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

**Three things stay separate *processes*** — different failure domains, not
gate flags a DB can flip: **embedder** (a model server the worker calls; must
be healthy before the worker boots), **web** (uvicorn), and **asa-bot**
(Discord bridge — our only Discord interface; stays, but as `deploy` once
the token is vaulted). These appear in the console as rows (health,
last-error, raw-I/O) but their control is start/stop, not `prio`.
*(Refined in §15b: the embedder is a separate process but a worker-supervised
**subprocess**, not a launchd/systemd-managed daemon — so the managed-daemon
count is worker + web + asa-bot = **3**, embedder as the worker's child. §15b
also states the criterion — distinct failure domain, three flavours.)*

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
process — a worker-supervised subprocess per §15b, not a managed daemon),
**web** (uvicorn), **asa-bot** (Discord bridge — our only one), **llama-swap**
(VRAM swapper), and the physical/heavy-dep capabilities (GPU, container images,
mounts — *provisioned* by ansible, only *gated* by the DB). Also retiring but
missed above: **`redis`** (litellm's only consumer — goes with litellm, §15a)
and the **14 `PRECIS_*_ENABLED` flags** (→ capability × prio, §15e).

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
   *Sub-sliced* because it changes the concurrency-critical claim path:
   **6a** prio+age claim ordering (replaces `ORDER BY ref_id`; the
   dispatcher propagates the parent todo's prio onto the minted job) *[BUILT]*;
   **6b** `resource_slots` table + heartbeat self-probe
   + console chips (dark — populate only) *[BUILT]*; **6c** reserve-at-claim
   for jobs that declare `requires` (opt-in; release on terminal + sweeper)
   *[BUILT]*; **6d** *[PARTIAL — BUILT]* requires-derivation from the
   job_type registry (activates gpu reservation for `struct_relax`/`fold`),
   reservation host = `target_node`-or-local, self-gating (unadvertised
   requirement falls back to the pin — no deploy stall), and the
   unschedulable alert. `target_node` stays as the node gate + advisory
   cache-affinity hint (not retired). *[BUILT — 6d-complete]:* the
   **capability-rarity ordering term** (`_scarcity` — the claim over-fetches
   `limit×3` and re-ranks `scarcity DESC, prio DESC, age ASC` in Python, so a
   rare-capability job leads; a no-`requires` queue collapses to the pre-6d
   prio/age order) and the **soft memory-pressure signal** (heartbeat
   `probe_soft_signals` → a `kind='soft'` `mem` gauge via
   `Store.sync_soft_signal`, written free-first; the claim **vetoes**
   requires-bearing jobs on a host whose `mem` free hit 0 — the jetsam guard).
   Both dark until a job carries `requires` / a host reports pressure.
7. **litellm retirement.** `served_by` on `llm` cards seeds
   `resource_slots`; capability-gated inline-LLM passes call localhost;
   the router pooling provider (only if any remote call survives);
   decommission the litellm daemon; llama-swap stays.
8. **Per-todo envelope.** `meta.envelope` honored by the executor across
   the three enforcement tiers (disallowed-tools / read-only DB role /
   no-net sandbox). *[BUILT]* — `workers/envelope.py` resolves the
   `{egress,write,return}` box (dark default = permissive) into the three
   tier decisions; **tier 1 is enforced live**: the `claude_inproc` executor
   wraps a job run in `envelope_scope(parse_envelope(meta))` and
   `call_claude_agent` merges the envelope's deny list into `--settings
   permissions.deny` (+ exports `PRECIS_MCP_DB_ROLE`). **Tiers 2 (DB role
   `agent_ro`) and 3 (`--network none` / api-only allowlist) are resolved
   but consumed by §13's per-call container executor** — the resolvers ship
   now, the enforcement lands with §13. `fix_gripe`'s own `_spawn_claude`
   also defers to §13 (it's the container tier).
9. **Quests tab.** Parameterize the fair-share window; close the
   quest-cost attribution gap; render the DAG + prio + share bar.
   *[BUILT]* — `allocator.over_budget`/`pick_next_quest`/`run_allocator_pass`
   take a `window_days` (env `PRECIS_QUEST_BUDGET_WINDOW_DAYS`, default 7d,
   `BUDGET_WINDOW_DAYS`) so the tab can tune 7d → 24/48h; the **cost gap
   (gripe 162594) is closed** — `quest_tick` now writes its real measured
   `res.cost_usd` to a `cost` logbook deed so `weekly_spend` (the tote) is
   honest and `over_budget` can actually fire; and `/factory` grows a
   **read-only Quests panel** (`_quests`) rendering each active quest's
   windowed spend vs its priority-weighted share as a bar (over-budget =
   red). *Follow-up (9b):* editable prio slider + enable/disable
   (STATUS active↔dormant) — both reuse the existing quest handler; and the
   `serves` DAG render.
10. **Single-plist collapse.** Fold worker variants + thin timers into
    one DB-controlled daemon per host (resolve the hermes/OAuth run-as
    question first). embedder/web/asa-bot stay separate.
11. **(Later) Model comparison / eval.** Run a task through model A vs B,
    score the outcome — the golden-task harness already stubbed in the
    `llm` catalog (`record_eval` is its write surface), surfaced on the
    model picker as "compare." *[BUILT]* — `src/precis/llm_eval/`
    (scorers → tasks → harness) runs a candidate over a gold set through the
    real `router.dispatch` seam and writes per-axis `measured-eval` ordinals
    via `record_eval` (the trust ladder's middle rung). **Deterministic axes
    wired now**: `long-context-recall` (needle) + `tool-structured`
    (tool_json); the **heavy axes** (`code` = run the fix's tests,
    `summarize-extract` = rubric judge, `reasoning-convergence` = prefer live
    telemetry) are declared but **skipped-with-a-log**, not silently scored.
    CLI `precis llm eval <model> [--compare B] [--gold PATH] [--no-record]`;
    seed gold set ships as package data (`data/llm_eval/gold_set.json`).
    *Follow-up (11b):* the web "compare" button on the model picker; wire the
    heavy-axis scorers; curated `scripts/llm_eval/gold_set/` drawn from real
    historical gripes/papers + endpoint-scoped `record_eval(quant=)`.
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

**Slices 0–5 SHIPPED + DEPLOYED** to the cluster (`main` at `c838295f`;
migration 0072 applied prod). **Slice 6b built + green in-container on
`worktree-zany-puzzling-thompson`, unshipped** (below). Slices 6a/6c/6d
and 7–11 remain.

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
* **Slice 6a (unshipped)** — prio+age claim ordering.
  `claim_executor_jobs` now `ORDER BY COALESCE(prio, 5) DESC, ref_id ASC`
  (was `ORDER BY ref_id`), and the dispatcher mints the child job with
  `prio = <parent todo's prio>` so a high-prio quest/project has its
  compute claimed ahead of commodity work; oldest-first is the within-band
  tiebreak. An all-unset queue collapses to the pre-6a FIFO. Capability
  rarity (§5.3) layers on in 6d. `service_config` per-service weight
  blending is deferred (this is the job's own DAG-inherited prio only).
* **Slice 6d (unshipped, partial)** — activates + hardens reservation.
  `effective_requires` derives a job's resource needs from its `job_type`'s
  `ServiceSpec.requires` (so `struct_relax`/`fold` reserve `{gpu:1}` with no
  mint change; explicit `meta.requires` still wins). The claim reserves on
  the job's `target_node` (the resource's host — an ssh_node GPU job
  reserves on the GPU box) or the local host, and **self-gates**: only a
  resource the reservation host actually advertises is reserved; an
  unadvertised requirement falls back to the node-gate/pin, so activating
  `requires` can't strand a job in the window before the probe populates
  `resource_slots`. The sweeper's `_alert_unschedulable_jobs` raises a
  `warn` alert for a queued job that needs a capability no host advertises
  *and* has no `target_node` fallback (pinned jobs still run, so they're
  skipped — no deploy noise). *Deferred:* capability-rarity ordering term
  and soft memory/load signals.
* **Slice 6c (unshipped)** — reserve-at-claim *mechanism* (still dark: no
  prod job declares `requires` until 6d). `store._resource_slots_ops`
  gains `reserve_resource_slots` (all-or-nothing conditional decrement —
  the lock) + `release_resource_slots` (capped refund). `claim_executor_jobs`
  reserves a job's `meta.requires` on the claiming host in the claim txn,
  stamps `meta.reserved`, and drops a job it can't serve here (lock frees
  at commit → waits for a host with capacity). `release_job_reservation`
  refunds at every terminal transition — hooked into `set_status` (executor
  paths) and the sweeper (crash recovery, which writes `STATUS:failed`
  directly). Idempotent + capped so a terminal/sweeper race can't inflate
  `free`. Hard discipline only; soft (memory) is 6d.
* **Slice 6b (unshipped)** — the resource substrate stands up *dark*.
  Migration `0073_resource_slots` (`host, resource, capacity, free, kind`,
  materialized counter — no separate lease table, crash-recovery reuses
  `meta.lease_until` + the sweeper). `workers/capability_probe.py`
  self-probes this host's capabilities (`gpu` via `nvidia-smi -L`,
  `podman`/`tts` via `which`/`find_spec` + env overrides), with the
  vocabulary *derived from* `ServiceSpec.requires` (present→advertise,
  absent→retract, unknown→leave). The `heartbeat` reporter syncs the
  verdict every cycle (`store.sync_host_resource_slots`, best-effort — a
  probe failure never breaks liveness) and `/factory` renders each host's
  slots as capability chips. **Nothing reserves the counter yet**, so
  scheduling is byte-identical to today — this is pure population +
  visibility. Reserve-at-claim (6c) and prio/scarcity ordering (6a/6d)
  are the behaviour-changing sub-slices, unbuilt.

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

## 15. The convergence — three layers by rate-of-change

> **Subsection map:** 15a complete role inventory · 15b the collapse criterion
> (3 managed units + embedder subprocess) · 15c the one config file · 15d drift
> (declared ⋈ discovered) · 15e retire the enable-flags (capability × prio) ·
> 15f the console row-set (declared ∪ discovered) · 15g big-files / storage ·
> 15h containers, install layout, run-as · 15i one scheduler (decentralized,
> DB-locked).

The organizing insight that unifies 12+10+7: the fleet repo grew *one
playbook + one role + one plist/unit per capability* — install it, declare
it, run it — so ~15 precis daemons became ~15 of each. But the "declare" and
"run" halves are exactly what slices 6+10 make the **DB and the worker** own.
Collapse the repo along its true rate-of-change seams:

1. **Provisioning recipes — shared, public, changes rarely.** Ansible roles =
   *how to install a capability* (llamacpp, ollama, GPU drivers, the container
   images, postgres, the precis wheel). Idempotent, secret-free, portable to
   any cluster. A role installs the *ability* to run a thing; it never decides
   *whether* it runs. This is the whole `deploy/` public tree.
2. **Placement table — private, tiny, changes per-cluster.** The *only* thing
   that differs between two clusters: for each machine, its **network facts**
   (tailnet / LAN IP, host role) and **which capabilities provision here**.
   This already exists, latent and scattered — `inventory/hosts.yml` +
   `topology.yml:precis_capabilities` (a capability→node map!) + small
   `host_vars`. The refactor **names it as one compact table** and makes it
   the entire private overlay. New cluster = write this table, nothing else.
3. **Runtime discovery — the worker, changes per-second.** Each node runs
   **one** worker that self-probes what is actually present (`capability_probe`
   → `resource_slots`, slice 6b) and runs what it is both *capable of* and
   *enabled for* (`service_config.prio`, slice 2). "See what's there, run it
   if it's there." No per-service daemon, no per-service plist — the thing
   that grew organically is exactly the layer that deletes.

**Why the placement table can stay tiny:** provisioning and discovery are the
same fact seen twice — the table says "spark has the GPU, install `dft`
there," the probe *confirms* `dft` is present on spark and the worker runs it.
The table declares the **provisioning target**; the worker discovers the
**runtime truth**; the DB gates the **live prio**. Drift between them is a
visible console state (grayed cell with a reason, §7), not a silent failure.

### 15a. What the ansible sprawl rationalizes to — the complete inventory

All 49 roles / 49 playbooks, classified so **nothing is dropped in the move**.
Three layers plus two piles (trim / triage). Every role is named exactly once.

**Layer 1 — Infra provisioning (20 roles).** The genuine "these are machines"
layer; stays as portable `deploy/roles`, install-only, no precis coupling:
`users`, `tailscale`, `nfs_server`, `nfs_client`, `autofs_client`, `postgres`,
`pgbouncer`, `pgpass`, `nginx`, `hardening`, `tcc_profile`, `ssh_tunnels`,
`monitoring`, `node_exporter`, `api_monitors`, `logrotate`, `backups`,
`config_pull`, `nightly_reboot`, `ansible_callback`. **(`redis` is NOT here —
see below; it retires with litellm.)**

**Layer 2 — Capability provisioning (13 roles).** Each keeps the role that
*installs the binary/image/model*; each **loses** the plist/unit + gate that
used to *declare + run* it → becomes a probed capability + `service_config`
prio, discovered by the worker:
`llamacpp` (llama-swap stays), `ollama`, `dft`, `aizynth`, **`catpath`**,
`alphafold`, `tts`, `alchemi`, `precis_eda`, `mcps` (external MCP venvs —
rewire off "Hermes agent profiles" post-hermes), `sortie` (sortie-mcp, external
`git+https`), `claude_code` (the `claude` CLI — **folds into the §13
`precis-agent` image** once the container is the default executor).
**`litellm` retires entirely** (§6/§7a) — not moved, deleted.

**Layer 3 — Precis run layer (15 roles → 3 managed units).** Collapse per §15b:
* **Stay as managed daemons (3):** `precis_worker`, `precis_web`, `asa_bot`.
* **Embedder → worker-supervised subprocess** (§15b): `precis_embedder` (plist
  deleted; worker discovers `bge-m3`, launches it, waits `/readyz`).
* **Fold into the worker's own scheduler as DB-gated passes/timers** (slice 10;
  the passes already live in the `ServiceSpec` registry): `precis_worker_agent`
  (→ the worker's *agent profile/capability*, not a daemon), `precis_heartbeat`
  (→ the worker's per-cycle probe+report), `precis_dream`, `precis_cron_tick`,
  `precis_watch`, `precis_watch_poll`, `precis_anki_sync`, `daily_briefing`,
  and `precis_reconcile` (folds **but stays caspar-pinned**). Plus the roleless
  timer playbook `22-papers-sync.yml`.

**Retires with litellm (`redis`).** Precis uses no redis at all (its queue is
postgres `FOR UPDATE SKIP LOCKED`, ADR 0007 — "not celery"). `redis`'s *only*
fleet consumer is `litellm_config.yml.j2` (router-coordination state + response
cache). So when litellm retires (§6/§7a) `redis` goes with it — the `redis` role,
the daemon, and `vault_redis_password`. Not infra to keep; a litellm dependency.

**Trim — not published, deleted (1 role + scaffolding):** `ollama_purge`
(one-shot Ollama cutover) + `99-ollama-purge.yml`; `tmp_*.sh` / `tmp_*.sql`;
the one-shot `run-fix-metadata.yml` / `run-migrate-refs.yml` / `run-reconcile.yml`
+ `reconcile.sql`; the already-retired `33–36` stubs. Fix the numbering
collisions (`04`, `22`, `30`, `40`, `42` each doubled) by renumbering into bands
that show the layering (00–19 infra · 20–39 capability · 40–49 precis run).

**Resolved placement (2026-07-17):**
* `extract_watch` — acatome-extract is an **external sibling tool**
  (`/opt/mcps/extract/venv/bin/acatome-extract`, its own venv + `~/.acatome/
  config.toml`, the `acatome` project — *not* the precis wheel) that shares
  precis's Postgres and feeds it. So it is a **Layer-2 discovered capability**,
  **not** a folded precis pass (it's foreign code). But its standalone
  `extract-watch.plist` **still collapses via the embedder pattern** (§15b): the
  worker supervises `acatome-extract watch` as a **subprocess** where the binary
  is present and `prio > 0`. *Verify before the window:* precis has its own
  `watch` pass on the same inbox — confirm acatome-extract isn't already
  superseded (two watchers, one inbox); if redundant, retire it outright.
* `mcps` + `claude_code` — **shrink to "install external MCP venvs only"**
  (sortie / catpath-mcp / remarkable, `git+https`). The §13 `precis-agent` image
  bakes the `claude` CLI + precis MCP, so the on-host claude/hermes install goes
  away post-cutover.

**Top-level files that move to `deploy/` (portable, public):** `site.yml`,
`redeploy-precis.yml` (rewritten for install-from-tree), `ansible.cfg`,
`bootstrap-macos.yml`, `bootstrap-tailscale.yml`, `requirements.yml`,
`callback_plugins/`, `populate-secrets-vault.yml` (the slice-0 tool), `README.md`.

**The overlay — stays private, gitignore-cloned into `deploy/inventory/`:**
`inventory/hosts.yml`, `inventory/group_vars/all/{vault,topology,precis_env,
main}.yml`, `inventory/group_vars/precis_anki_sync/{vars,vault}.yml`,
`inventory/host_vars/*.yml`, `.vault-pass`.

**Non-role moving parts (easy to miss — enumerated so they don't strand):**
* **Daemon templates** — the run-layer collapse is provable from the actual
  installed units: `precis-{worker,worker-agent,web,embedder,dream,cron-tick,
  anki-sync,heartbeat,reconcile,watch,watch-poll}.plist` + `com.asa.bot.plist`
  + `extract-watch.plist` = **13 precis daemons → 3** (worker/web/asa-bot),
  embedder → subprocess, the rest → folded passes.
* **`laptop/`** — the *deploy laptop's* own autossh tunnels into the cluster
  (`com.tunnel.{caspar,melchior}.plist` + `install.sh`). Operator-machine
  config, not a cluster node: portable half → `deploy/laptop/`, tunnel *targets*
  (IPs) are overlay facts.
* **`tasks/reload_launchd.yml`** — shared task include → `deploy/tasks/`.
* **`mcps` role installs `code-sandbox.{plist,service}`** — the podman sandbox
  MCP that backs `job_claude_docker` (`requires: podman`); a capability daemon,
  folds under §13's container work.

**The `ServiceSpec` registry is the single source of truth that proves the fold
drops no work.** Every folded daemon corresponds to a pass already in
`src/precis/workers/registry.py`, which keeps running inside the worker:
`precis_dream`→`dream_agent`, `precis_watch`→`watch`, `precis_watch_poll`→
`watch_poll`, `precis_reconcile`→`corpus_reconcile`+`paper_reconcile`,
`precis_cron_tick`→`schedule`, `daily_briefing`→`briefing`+`news_poll`,
`precis_worker_agent`→the agent-profile passes (`structural`, `deep_review`,
`job_claude_inproc`, `dream_agent`), `precis_heartbeat`→the worker's per-cycle
probe. **Two to confirm-or-add when folding:** `precis_anki_sync` and the
roleless `papers-sync` — verify each has a registry pass (or register one)
before deleting its daemon.

Net: the per-capability *triple* (install + declare + run) becomes install-only
in ansible; declare + run move to the placement table (§15c) + the discovering
worker. 15 run-layer roles → 3 managed units; `litellm` + `ollama_purge` gone.

### 15b. The collapse criterion — distinct failure domain, not "precis-related"

The ~11 daemons that fold do so because they are **the same process shape as
the worker**: the identical binary, either one-shot invocations (`precis cron
tick`, `precis reconcile`) or claim-loop passes, split into separate timers
only because that is how they grew. Folding them removes real complexity — one
scheduler, not eleven timers. What *survives* survives because it is a **distinct
failure domain**, and there are three distinct flavours of that (it is emphatically
**not** "all resident" — residency is only the embedder's flavour):

* **worker** — *is* the claim-loop. The one thing that discovers + runs passes.
* **embedder** — **residency**: a resident model server (bge-m3, multi-GB), a
  dependency others block on; reloading costs fleet-wide no-embedding. But its
  separateness is *process-level, not management-level* → it is a worker-
  supervised subprocess (§15b-below), so it does **not** count as a managed unit.
* **web** — **blast-radius + placement**: cheap to restart (≈stateless uvicorn),
  but a hung/OOM pass must not take the `/factory` console down with it, and it
  is gateway-only (one node) while the worker runs everywhere. Isolation, not
  residency. (The one genuinely arguable fold — if you gave up the console-
  survives-a-pass-crash guarantee you could push it in; we keep it out.)
* **asa-bot** — **foreign package**: a different codebase (`src/asa_bot/`, the
  `[asa]` extra) that *talks to* precis over MCP, with its own failure modes
  (Discord outages, identify rate-limits, token refresh) and deploy cadence.
  Folding it means the worker imports a Discord bot and their releases couple —
  worse coupling. A packaging fact the plist collapse doesn't touch.

**The embedder is discovered, not declared.** The worker sees `bge-m3` installed
(§15.3 discovery), launches the embedder locally, and waits for **`/readyz`**
(never `/healthz` — it lies "warming" until the model actually loads) before
running embed passes. This deletes the `precis_embedder` plist *and* the
"restart-embedder-first → poll health → then bounce dependents" dance in
`redeploy-precis.yml` — the ordering becomes internal to the worker's own
bring-up. v1: a plain supervised child (a worker restart reloads the model, but
that is ≈only on deploys, which stop-the-world for that node anyway, and search
degrades to lexical in the gap via the embed guard). Later, if non-deploy worker
restarts prove costly, give the embedder an **independent restart lifetime** (the
worker `ensure-up`s it idempotently and re-attaches to the warm process). So
"separate process" ≠ "separately-managed daemon": the embedder is the former,
not the latter — hence **3 managed units, not 4**.

### 15c. The one config file — the cluster's whole description

`deploy/{roles,playbooks}` ship with precis and are identical on every cluster;
the **only** per-cluster artifact is the Ansible **inventory** — the standard
"portable playbooks, site-specific inventory" split. Authored host-centric to
match how an operator thinks ("what does this box do"):

```yaml
# deploy/inventory/hosts.yml — the ONLY file that describes THIS cluster.
all:
  hosts:
    melchior:
      ansible_host: 100.x.x.1              # network fact: tailnet IP
      services: [worker, worker-agent, web, asa-bot, bge-m3]
    caspar:
      ansible_host: 100.x.x.2
      services: [worker, postgres, bge-m3, reconcile]
    balthazar:
      ansible_host: 100.x.x.3
      services: [worker, bge-m3]
    spark:
      ansible_host: 100.x.x.4
      services: [worker, bge-m3, dft, mace, alphafold, chem-route, tts]
```

Everything derives, no custom code:

* **Capabilities → install.** Ansible's built-in **`constructed`** inventory
  plugin turns each per-host `services` list into groups, so `hosts: dft` and
  `when: "'dft' in services"` both work and `ansible-inventory --graph` gives
  the service→hosts view for free.
* **The postgres singleton.** Just a service that lands on one host;
  `PRECIS_DATABASE_URL` for *every* node derives from `groups['postgres'] |
  first` → `caspar:6432`. One source of truth, no per-host URL copy.
* **Servers, each separately.** `web` and `asa-bot` are distinct tokens (so
  "web here, asa there" is expressible); a lint constrains each singleton
  (postgres/web/asa-bot) to exactly one host.
* **Workers → discovery.** Every host with `worker` runs the claim-loop and
  probes what is actually installed; `bge-m3` present → it supervises the
  embedder; `dft` present → it claims dft jobs.

**The config is a floor, not a cage.** Declared capabilities are provisioned +
expected (absence = drift, below), but a worker that *discovers* an extra
capability still uses it — "install a tool by hand on a box and it just starts
working" stays true, which is the whole discovery premise. Real IPs/hostnames
live only in this overlay file, never the public `deploy/` tree (§14 leak guard).

### 15d. Drift — declared (deploy) ⋈ discovered (probe), joined in the DB

The inventory lives on the deploy laptop and never travels to the cluster. **The
deploy is the moment the laptop's intent becomes the cluster's recorded intent:**
each host, in its own deploy play, self-declares its `services` (from hostvars)
into the DB. Both sides of drift then live in the DB, written by different
actors at different cadences:

| | table | written by | cadence |
|---|---|---|---|
| **declared** (what *should* be here) | `declared_capabilities` | the deploy, from inventory | at deploy |
| **discovered** (what *is* here) | `resource_slots` (slice 6b) | the worker's probe | every heartbeat |

Drift is a pure **DB-vs-DB join** — web computes `declared − discovered` and
renders the console: a declared-but-absent capability (provisioning failed or
regressed) is a **grayed cell with a reason** + a `warn` alert (`precis service
audit` as the CLI form). The inverse — discovered-but-undeclared — is fine (the
floor): the probe just advertises it. Web never reads the laptop, never needs
the overlay file, never needs the laptop reachable.

**The semantics this buys are the right ones:** drift = "reality diverged from
the last deploy." Edit the inventory but don't deploy → the DB still reflects
the *running* intent, so it correctly shows **no drift** until you apply. The
inventory file is a deploy-time *writer*, never a runtime dependency — the
laptop is source-of-truth, the DB is the published snapshot (the git-vs-running
relationship, applied to placement). Keep `declared_capabilities` a dedicated
table (deploy owns it entirely) rather than overloading `service_config` (which
stays purely the console-owned live `prio`); the one mapping to nail is *service
name* ↔ *probe capability token*, which `ServiceSpec.requires` already carries.

### 15e. One control surface — retire the `PRECIS_*_ENABLED` flags

"Too many switches in the kitchen." Today a pass can be gated **three** ways —
the `PRECIS_*_ENABLED` env flag (in a plist / host_var / group_var), the plist's
very existence, and (post-slice-2) `service_config.prio`. There are **14 such
flags** live — `PRECIS_{ANKI,ANKI_FIX,ANKI_PROJECT,BACKLOG_GROOM,BIO,
BRIEFING_AUDIO,CAST_AUDIO,CHEM,CLASSIFY,LLM_RECONCILE,PAPER_GLOSSARY,QUEST_LOOP,
SANDBOX}_ENABLED` + `PRECIS_CATPATH_ENABLED` — and the worst (`CATPATH`×9,
`CHEM`×6, `BIO`×6) are copy-pasted across many host files. Each conflates two
questions the new model separates cleanly:

* **Can it run here?** → **autodiscovery** (`capability_probe` → `resource_slots`).
  Catpath not installed → the probe never advertises it → grayed in the console,
  cannot run. This replaces the flag's "is the dependency present" half.
* **Should it run here, at what weight?** → **`service_config.prio`**, set from
  the `/factory` console, `0 = off`. This replaces the flag's "do I want it on"
  half — live, no redeploy, no `edit → re-render plist → bootout` cycle.

So the two orthogonal axes — **capability (discovered) × prio (console)** — are
the *entire* control surface; the 14 env flags are the redundant third switch
and are **deleted** (slice 10). This finishes what slice 5 started (which
universalized the *incidental* patent/edgar gates): 5 killed the "unavailable
because a key/dep is missing" gates; 10 kills the "enabled?" toggles. After
both, an operator turns a pass on/off in one place — the console — and the
worker refuses only what the box genuinely can't do.

### 15f. The console row set = declared ∪ discovered (the superset)

With the enable-flags gone (§15e), control is two axes: **capability (can-run,
discovered)** × **prio (should-run, console)**. For the console to hang a prio
knob on a service, that `(host, service)` must be a *row* — and the row set is
the **superset** of what we declared and what we discovered, because both the
missing and the surprise matter. It's a full-outer-join filtered to "relevant
to this host":

```sql
SELECT s.service, (d.host IS NOT NULL) AS declared,
       (r.host IS NOT NULL) AS discovered,
       COALESCE(sc.prio, s.default_prio) AS prio
FROM   registry_services s
LEFT   JOIN declared_capabilities d ON d.host=:h AND d.service =s.service
LEFT   JOIN resource_slots         r ON r.host=:h AND r.resource=s.token
LEFT   JOIN service_config          sc ON sc.host=:h AND sc.service=s.service
WHERE  d.host IS NOT NULL OR r.host IS NOT NULL;   -- ← declared OR discovered
```

Row state falls out of the two booleans: **both** → prio editable, runs;
**declared∧¬discovered** → drift, grayed, prio locked 0, reason; **¬declared∧
discovered** → undeclared/floor, prio editable, badge. The prio knob writes
`service_config`; editability is gated by `discovered` (capability wins — you
can't set positive prio on what the box can't run). **Safety default:** a
discovered-but-undeclared capability defaults to **prio 0**, never full weight —
discovery makes it *possible*, the console makes it *active*, never the reverse.

### 15g. Big files / models — fileserver-canonical, digest-staged

Large immutable model files (bge-m3, marker, mace/dft potentials, alphafold
weights, GGUFs) follow **one** rule: **canonical copy on the fileserver,
digest-pinned → rsync-staged to local SSD → loaded local.** Never mmap'd over
NFS; never re-downloaded per-node from the internet. The GGUF store already does
this (caspar NFS → local SSD, a size-annotated manifest); **bge-m3 today does
NOT** (each node pulls ~2.3 GB from HuggingFace at first boot) and should move
onto the fileserver pattern — one download, reproducible, offline-capable.

*How to specify a big file* — never per-host. A small **models manifest** split
by portability: **identity** (`bge-m3 → {source, revision/sha256, size}`) →
portable role default (same everywhere); **location** (where the fileserver
keeps it) → the overlay storage stanza (derived from `nfs_export_path`/
`nas_root`). A host acquires it by declaring the *service*; the role stages
canonical→SSD **by digest**. Delivery has two forms, same manifest: on-host =
NFS→SSD stage; container (§13/§15h) = a baked image layer (`:premodels`). Two
discovery hooks: **"staged & sha-verified" is a probed capability** (a half-synced
model = grayed, no claims — verify by digest, not file-presence), and **free-disk
is a soft signal** (an 88 GB GGUF decides who can even hold it).

*The shared fileserver itself is site config* — a small storage stanza in the
overlay: `nfs_server`, `nas_host`+`export`, per-**OS**-group mount roots
(`/opt/nas` macOS · `/nas` Linux) + rare per-host override. All paper/project
paths *derive* from one `nas_root`; the mount is an OS fact, not a per-machine
line. Bonus: **"NAS mounted & writable" is a probed capability** so a stale mount
vetoes NAS-dependent claims (the jetsam-guard pattern, applied to storage).

### 15h. Containerization, install layout, and run-as

**Containerize what we can, ansible-managed.** Five roles already run in
containers (`aizynth`, `alphafold`, `dft`, `mcps`/code-sandbox, `tts`) — all
Linux/spark compute. Generalize: a Layer-2 capability's provisioning becomes
"**ansible ensures a digest-pinned image is present; the worker runs it**" —
a long-running container *unit* for warm servers, `docker run --rm` per-job for
one-shot compute (the §13 pattern; **reservation = container concurrency**, one
`resource_slot` = max concurrent containers). Payoff: reproducible toolchain
(the digest pins everything), no host contamination (heavy chem/bio deps stay
off the host venv), a clean probe (image present ⇒ capability), and it unifies
with §15g (models as baked layers) and §13 (the agent image).

**The podman dependency is a `requires` capability, not a global mandate.**
Container-jobs already declare it (`job_claude_docker requires podman`; the probe
detects podman via `which`). So podman is **implicitly provisioned only where a
container-capability is placed** (derived from `requires` — the config never
hand-lists it), **discovered** by the probe, and the claim **gates** container
work on it; declared-but-absent podman is the drift alert. "Force it in the host
list" is only the deliberate opt-in.

**The macOS boundary is the crux.** Docker on macOS is a heavy Docker-Desktop/
colima **Linux VM with no GPU passthrough** (Apple MPS is invisible to Linux
containers). So the honest rule: **containers are a Linux-node thing** (podman
native, GPU via nvidia-container-toolkit — which is why the dockerized set is all
spark); **macs stay native** (`alchemi` uses MPS directly; worker/embedder
native). We do **not** auto-install a Docker VM on every mac. Consequently
§13's "container is the default agentic executor" means *default where podman
exists*: slice 0 de-pinned agentic work from melchior, so **agent containers run
on Linux nodes**, and a host without podman **falls back to the in-proc
`claude_inproc` executor** (tier-1 envelope only — tiers 2/3 need the container).
Graceful degradation through the same capability gate.

**One shared `service_unit` role — the missing multiplatform abstraction.**
Today platform-branching is a custom `os_family` var (set `darwin`/`linux` on
the macos/linux inventory groups) + inline `when: os_family == …` guards,
**duplicated across 6 dual-template roles** (`llamacpp`, `mcps`,
`precis_embedder`, `precis_heartbeat`, `precis_watch`, `precis_worker`) with only
a shared `tasks/reload_launchd.yml` include. There is **no** single role that
owns multiplatform. Build one: a `service_unit` role taking an abstract spec
(name, exec, env, schedule, run-as) that renders the **launchd plist OR the
systemd unit** by `os_family`. Each capability role then delegates unit-rendering
instead of carrying two templates. **This is exactly what slice 10's "single
plist" is** — "one plist" is really "one service *definition*, two renderings."
Containers compound the win: a containerized capability is inherently
cross-platform (same image), so only the launch-unit differs — which
`service_unit` handles, shrinking a capability role to a manifest entry.

**Install layout — the dedicated `/opt/precis` tree, not system defaults.**
Today: `/opt/precis/venv`, `/opt/precis/embedder-venv`, `/opt/mcps/venv` (uv at
`/opt/homebrew/bin/uv` mac · `/root/.local/bin/uv` Linux). Keep it: a
self-contained precis-owned tree (add `/opt/precis/bin` for binaries), never
scattered into `/usr/local`. Wipeable, no host contamination, clear ownership,
matches install-from-tree (slice 12) and the container's `/app`+`/opt/venv`.

**Run-as — one identity (`deploy`) for now.** Today it's mixed (`deploy` for
most, `hermes` for agentic); slice 0 collapses all to `deploy`. Do **not** add a
`precis-operator` service user now — slice 0's whole point is to *dissolve* an
identity boundary, and a new run-as re-introduces one. The trust boundary that
matters is **not the UNIX user** — it's the **DB role** (`agent_ro`/`agent_rw`,
§4) + the **container** (network/isolation, §13). Least privilege lives there. A
dedicated deploy-installs / lower-priv-runs split is a *later* hardening step, if
ever — deferred, not now.

### 15i. One scheduler — the single recurring-work trigger

Today recurring work has **two** triggers, and they overlap:
* **`schedule`** — a `_SYS` worker pass, *"mint subtasks for due `level:recurring`
  Watches"* (the todo-tree's recurrence).
* **`cron`** — a first-class kind (`kind='cron'`: `next_fire_at` + recurrence +
  `catch_up`), fired by the **`precis cron tick`** launchd timer, which
  `pg_notify`s and advances `next_fire_at`.

**Decision: there ought to be only one scheduler.** Two consequences:

1. **One mechanism, not two.** `schedule` and `cron` are the same job — "fire a
   thing when it's due, advance its next-due, honor catch-up." Converge them onto
   **one recurring engine** (the `cron` kind's `next_fire_at`/`catch_up` model is
   the more complete one; `level:recurring` Watches become entries in it). One
   recurrence model, one place to reason about "what fires when."
2. **One *firing* per due entry — guaranteed by the DB, not by a designated
   node.** "Only one scheduler" is a claim about *outcomes* (each recurring entry
   fires exactly once), and the exactly-once guarantee belongs in Postgres, not
   in a single host (which would be a SPOF — down when a mint is due ⇒ missed
   fire). So **minting is decentralized**: every worker runs the scheduler pass
   each cycle, and claiming a due entry is an **atomic conditional advance** —

   ```sql
   UPDATE cron SET next_fire_at = <next>, last_fired = now()
   WHERE id = :e AND next_fire_at <= now() RETURNING id;
   ```

   Only one worker's `UPDATE` matches the row (the rest see it already advanced) —
   **the advance *is* the lock**, exactly the reserve-at-claim pattern (§5.2). So
   there is **one logical scheduler with no physical singleton**: decentralized,
   exactly-once, and a worker being down never drops a fire — any other live
   worker mints it. (An advisory-lock or `FOR UPDATE SKIP LOCKED` variant works
   equally; the conditional-advance is the least machinery.)

**Trigger is separate from execution.** The scheduler only *mints* the recurring
jobs; each minted job then **routes to the right node by capability** (an agentic
`briefing`/`quest_tick` job flows to the host advertising the agent capability;
a GPU job to the GPU host) via the normal claim path. One logical clock,
decentralized hands — §5's decentralized-claim intact end to end.

**Catch-up is now the only-if-*everyone*-was-down backstop.** Because any live
worker mints, a fire is dropped only if the *entire fleet* was down when it came
due — and `cron`'s **`catch_up`** policy fires those on recovery (late, not
lost). No designated-node SPOF to mitigate. (If you ever *want* to pin the tick
to one host for other reasons, it's still just a `prio` cell — but it's an
option, not a correctness requirement.)

This also **kills the `precis_cron_tick` launchd timer** — it folds into the
worker as the decentralized `scheduler` pass (slice 10), like everything else.

## 16. Build plan — 12 · 10 · 7 · 13 in one maintenance window

**Decisions locked (2026-07-17):** overlay stays **local-only** (`~/work/cluster`
keeps its inventory/vault/`.vault-pass`, gitignore-cloned into
`deploy/inventory/`; push to a private remote is a deferred follow-up); the
**container is the default agentic executor** (§13, fleet-wide, not opt-in);
`scripts/deploy` **flips to install-from-tree inside the window**. The cluster
**goes offline** for the cutover — a maintenance window, not a live cutover, so
ordering collapses to *stop → migrate → start → verify → rollback-if-red*.

### Phase 1 — dark code (ships to `main` + deploys via the current git+https mechanism, live, no window)

Each is byte-identical to today until the window flips it on. Ships green the
normal way; the already-built 8/9/11/6d batch ships in the same train.

* **12a — repo rationalization + `deploy/` tree.** Trim cruft; renumber; draw
  the overlay boundary **per line** (overlay = network facts + placement table
  + secrets + per-host numeric knobs; everything else → `roles/*/defaults`);
  emit `.example` contract files; add the CI grep-gate that **fails the public
  tree** on any `$ANSIBLE_VAULT` header, `100.x` address, real hostname, or a
  `{{ vault_* }}` reference missing from `vault.yml.example`. Move the portable
  tree to `deploy/`; add install-from-tree to `scripts/deploy` **dark** (keeps
  deploying via git+https until the window). Validate with `ansible --check`
  against the live fleet.
* **7-code.** `served_by` on `llm` cards → seeds `resource_slots`; inline-LLM
  passes become capability-gated to localhost. Dark: litellm endpoint config
  untouched, so routing is unchanged.
* **10-code.** The worker scheduler learns the thin-timer cadences behind a
  flag; the single collapsed-plist template is authored in `deploy/roles`.
  Dark: flag off, timer daemons still own the ticks (no double-fire).
* **13-code.** `precis-agent` image (wheel + CLI + skills, digest-pinned) + the
  container executor path, **policy-gated off**; envelope tier-2 (`agent_ro`
  DB role) + tier-3 (`--network` allowlist) enforcement wired to the same
  `docker run` knobs.

### Phase 2 — the window (ops, one sitting; ordered for verifiability)

1. **Stop the fleet** — every precis daemon down on every host.
2. **Vault the OAuth token** (slice 0 §12 steps 1–2): `precis secret set
   CLAUDE_CODE_OAUTH_TOKEN`; verify each agent host reads it + a `claude -p`
   smoke authenticates via the vault leg.
3. **Flip run-as → `deploy`** (worker-agent / dream / asa-bot); scope
   vault-read on that secret tightly. **Leave `~hermes/.claude` on disk** as
   rollback insurance (delete the principal only after days-green).
4. **Collapse the plists** (slice 10): install the single `precis-worker`
   daemon, remove the four; fold the timers in (flag on); `reconcile` stays
   caspar-pinned; embedder / web / asa-bot stay separate processes.
5. **Retire litellm** (slice 7): seed `resource_slots` from `served_by`,
   decommission the daemon, confirm no external consumer points at its port.
   *Transport-collapse prerequisite (Track 2), landing dark ahead of the
   window:* the **local** direct-`LlmClient` passes that bypassed the router —
   `llm_summarize`, `classify`, `paper_glossary` — now fold through
   `router.dispatch` via `DispatchClient` (`router.LlmRequest.max_tokens` lets
   `paper_glossary` keep its 2000-token budget; a per-chunk pass sets
   `log_call=False` so a corpus backfill doesn't add a row per chunk). Behaviour
   is byte-identical until `served_by` is seeded — then the same call reroutes to
   the host's llama-swap endpoint instead of the litellm proxy, and litellm loses
   its precis **local** consumers. The **cloud** direct-`LlmClient` passes
   (`reading/cards`, `workers/briefing`, `reading/meditation`,
   `reading/briefing_cast` — "claude-opus" via the litellm aggregator) fold
   through dispatch's cloud tier onto `claude_p` (direct Anthropic OAuth) next;
   that removes litellm's last precis consumers. Seeding `served_by` on prod
   cards (endpoint = llama-swap `:11445`, real model name) is the flip.
6. **Cut the deploy substrate** (slice 12): flip `scripts/deploy` to
   install-from-tree, deploy from the new `deploy/` tree, retire
   `precis_worker_git_ref` / `precis_web_git_ref`.
7. **Container = default** agentic executor (slice 13 flag on).
8. **Start + verify (the gates, each blocks the next):** heartbeats green →
   `claude -p` smoke on a **non-melchior** host (proves vault + `deploy`
   run-as de-pinned agentic work) → an inline-LLM pass hits localhost (proves
   litellm-free routing) → a container agent job completes (proves §13). Any
   gate red → **roll back** to the old plists + old `redeploy-precis.yml`
   (still on disk, local-only — the escape hatch is always present).

**The one irreversible risk is the public-repo leak** (12a); every other step
rolls back. Its guardrails — local overlay + gitignore + the CI grep-gate —
are built and proven **before** the first `deploy/` commit lands.

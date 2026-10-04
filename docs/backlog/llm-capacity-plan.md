---
status: draft
title: LLM capacity plan — what model runs where, on what memory and slots, against what demand
pillar: local-compute
prio: high
---

# LLM capacity plan — what model runs where, on what memory and slots, against what demand

Reto, 2026-10-02 (review item `reto-llm-capacity-1`): "We run LLMs on some
of the machines; we should make a plan of what runs where, for capacity
planning."

## Motivation / why

Three local-compute decisions are each being made without a shared picture
of the fleet: where the dedicated summariser slice goes
(`local-summarizer.md`), which model the single big-model slot carries
(`vllm-per-node-serving.md` Slice 0), and who owns embedder capacity
(`embedder-capacity-ownership.md`, td461158). Each needs the same table:
per node, what is resident, what it costs in memory, how many slots it
advertises, and how much demand would land on it.

## In scope

One backlog-doc table (this file grows into it), numbers from prod, nodes
named by **role/group only** (big Mac, SMALL Mac, DB node, GPU twins, spark,
external HPC). The role→host mapping and every address stay in the
gitignored overlay `deploy/inventory/hosts.yml`; this repo is public.

Per node:

- **Resident models**: llama-swap entries on the big Mac (model, quant,
  file size, ctx, `--parallel`, coexist group), GPU-twin serving
  (`spark-pair-big-model-serving` runbook values), embedders (bge-m3
  service, the session MCP's embedder), the SMALL Mac's model.
- **Memory**: unified RAM / VRAM footprint per resident model, KV cache at
  the configured ctx × parallel, and headroom.
- **Slots**: `resource_slots` `llm:*` capacity per host, checked against
  the server's real `--parallel` (the 6-vs-4 mismatch carried in
  `local-summarizer.md` is the failure this catches).
- **Demand**: `llm_call_log` by tier and source, local vs cloud, from
  `get(kind='llm', id='/placement')`, as calls/day, tokens/day and $/day;
  the summarise backlog size as the bulk demand that would move.
- **Competing compute on the same nodes**: catpath GPU lanes on the GPU
  twins, gate containers and the shared MCP serve on the big Mac (the
  2026-10-02 kernel panic was file-handle exhaustion under container load
  there), embed batches.

Planned additions, as rows with a status column:

- External HPC via slullama (`slullama-hpc-placement.md`; key registration
  pending with Reto, review items `reto-meluxina-1` / `local-compute-3`).
- spark is back on duty in the embeddings role (Reto 2026-10-02, review
  items local-compute-4/5), not in the `inference` group.

### Six machines, roles ruled 2026-10-02

Reto (review items local-compute-4 and local-compute-5, 21:03Z and 22:03Z):

| box | kind | role | must not carry |
|---|---|---|---|
| castor | Spark (GB10) | exclusive big model (Slice 0 picks it) | anything else once the model serves; retrosynth moves to pollux |
| pollux | Spark (GB10) | GPU science lanes: DFT, NEB/MACE, fold, retrosynth | LLM serving |
| spark | Spark (GB10) | local embeddings | science lanes, until the `/mnt/cluster` hang is fixed |
| melchior | Mac | dev + MCP server + gate containers | ad-hoc heavy compute (Reto: "don't wear it down") |
| balthazar | Mac (small) | scheduler; small local model | heavy compute |
| caspar | Mac | Postgres (+ the `/mnt/cluster` NFS export until it moves) | any compute (standing rule) |
| finnmaccool | file server | the cluster share, mounted directly by every node | — |

**Ad-hoc heavy compute** (one-off runs outside the job queue, e.g. the
catalysis PBE single points) goes to **spark**: at most half its cores,
local scratch only, no `/mnt/cluster` paths. Never melchior. Anything that
should repeat gets a job type on pollux. Ruled by Reto 2026-10-02 in review
item local-compute-7, which also confirmed the six-machine list.

**No Mac or Spark serves files** (same ruling). The share moves off caspar
to finnmaccool; see `cluster-fileserver-move.md`.

### Agreed layout plan, NOT scheduled (Reto 2026-10-03, local-compute-17)

Reto: "A is good but we just write it down for now, as a plan for later."
Nothing below is built until he reopens it. The 10-02 role table above
stands meanwhile, with castor as the exclusive big model.

The measurement it rests on (read-only, 2026-10-03):

**Demand.**
- Product LLM calls cost about $259/week. The big tier is $156 of that, and `dream` alone $111. Medium is $79 (`chase:verify` is most of it), small $22.
- No layout can save more than that, so hardware is not paid back by cloud savings. Local buys throughput (the ~586k-chunk summary backlog), independence from the provider, and privacy.

**Supply.**

| box | measured |
|---|---|
| one GB10 Spark, gpt-oss-120b on vLLM | 290 out tok/s at 32 streams, 400 at 64. About 15× the medium tier's ~1.7M out-tokens/day |
| big Mac (M2 Ultra, 192 GB) under dev-fleet load | glm-4.7-flash: ~35 tok/s aggregate at 8 streams. 80B-A3B Q4: 24–53 tok/s single stream |

- So the Mac fits the low-concurrency big tier, not the small tier.
- On castor, two 120B-class vLLM servers do not fit: 66.0 + 69.5 GiB of weights against ~121 GiB usable. One gpt-oss-120b server for both tiers does fit.

**The plan, in order. Every tier keeps its cloud model as the last chain rung, so a down box costs money and never fails.**
1. **A as a one-tier-at-a-time pilot.**
   - Medium on castor. This changes the 10-02 castor ruling.
   - Big-Mac hardening: drop the idle 27B from llama-swap, cap the gate VM, alert on Docker disk.
2. **A big-tier eval set** (dream / doctor_tick / quest_tick from `llm_call_log`), run on the ≤90 GB Mac candidates and on castor's gpt-oss-120b. The result picks the target:
   - **D**: castor serves big and medium from one gpt-oss-120b, and the Mac carries no prod tier. Chosen if gpt-oss-120b passes.
   - **G**: big on the Mac, medium on castor, gate containers moved to spark. Chosen if only a Mac model passes.
   - **B**: buy a dev machine and make the Mac LLM-only. Chosen only if a ~142 GB model (DeepSeek V4-Flash) passes where every ≤90 GB one fails.
3. **C**, a fourth Spark for medium, only if the science queue outgrows pollux plus the external HPC.

Rough cost: about 5 build cycles to the big-eval result. B adds 3–4 plus hardware; G's gate move adds 2–3.

## Explicitly NOT in scope

- Changing any placement, slot or chain row — the plan informs the three
  decisions above; each moves in its own item.
- Model quality — that is `model-qualification.md`.

## Acceptance criteria

- One table, every LLM/embedder served on the cluster plus the planned
  rows, each with node role, memory, slots, measured demand, and the date
  the numbers were read.
- Slot capacity reconciled against the server's real parallelism; every
  mismatch listed.
- For each of the three consuming decisions, one line on what the table
  says about it.
- `tests/test_deploy_tree_no_secrets.py` stays green (no hostnames or
  addresses).

## Target + blast radius

Docs only. Reads: prod `resource_slots`, `app_settings` `llm.chain.%`,
`llm_call_log` (via the placement view), per-node read-only digests
(llama-swap `/v1/models` + config, `nvidia-smi`, memory).

## Open questions / decisions log

- 2026-10-02 13:17Z read (the first rows): big Mac llama-swap serves
  `glm-4.7-flash` Q5_K_M (20 GB), `qwen3.6-27b-q8_0` (27 GB) and
  `qwen3-next-80b-a3b-q4_k_m` (45 GB), ctx 131072, all idle; GPU twins
  serve nothing; DB node holds a `llm:deepseek/deepseek-v4-flash` slot
  (cap 4) with no server confirmed behind it; SMALL Mac holds
  `llm:qwen3.6-35b-a3b-ud-q3_k_m` (cap 1). Every chain row is cloud. The
  GPU twins' staged-weight listing timed out on the shared mount and is
  still unread.
- 2026-10-02 the DB-node `llm:deepseek/deepseek-v4-flash` row, resolved:
  **keep it, and it currently gates nothing.** It became the fleet-wide
  big-LLM semaphore through the remote-serving path in
  `utils/llm/local_serving.py::acquire`: a dispatch on any host reserves
  against the row named by a LAN-routable `served_by` entry, so a
  `served_by` entry carrying the DB node as its accounting host made this
  row the shared cap for the GPU-twin pair. Today the model's prod `llm`
  card (lm162511) shows no `served_by`, so `acquire` finds no remote entry
  and returns `None` before touching the row. It cannot mark a call local
  either: the router stamps `local` only when a reserved slot carries an
  endpoint. It comes back into force the moment the twins re-advertise the
  model with that accounting host. Do not delete it (standing ruling; the
  cap is the twins', the host label is history). Caveat: read through the
  MCP card view, which may not render an empty `served_by`; the check is
  `refs.meta->'served_by'` on lm162511.

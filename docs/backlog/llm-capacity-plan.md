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
- spark stays **off** cluster duty (standing ruling); listed so nobody
  re-adds it from this table.

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

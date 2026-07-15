---
id: precis-route-help
title: precis — the route kind (retrosynthesis routes you read as a graph)
summary: plan a synthetic route to a target molecule with a swappable engine (stub/aizynth/askcos) on the compute lane, content-addressed so a repeat is a zero-compute cache hit; read the route as a step graph or its LinChemIn descriptors (view='metrics') — never a synchronous planner call
applies-to: get/put/delete (kind='route')
status: active
---

# precis-route-help — retrosynthesis the LLM can *read*

A `route` is a **retrosynthetic plan to a target molecule** — a shallow DAG of
disconnections (product ⇐ precursors), normalized to one IR no matter which
engine produced it (ADR 0056). It is the chemistry sibling of the keystone
kinds (`structure`/`cad`/`pcb`): the LLM traverses a **graph + numbers**, never
runs a planner in the request path. A `route` is a plugin kind (precis-chem),
**dark behind `PRECIS_CHEM_ENABLED`**.

Slug-addressed, three verbs (plus `tag`/`link`): `put` (plan / cache-hit),
`get` (list / render / metrics), `delete` (soft-retire).

## put — plan a route

```
put(kind='route', id='aspirin', target='CC(=O)Oc1ccccc1C(=O)O', engine='aizynth')
```

- `id=` — the route slug (required). `target=` — the product **SMILES** (required).
- `engine=` — the planner (default `stub`):
  - **`stub`** — a deterministic, chemistry-free toy planner. No cluster, no
    deps: it just exercises the substrate (mint → plan → cache). Use in tests /
    when no compute node is configured.
  - **`aizynth`** — AiZynthFinder, a **container** engine on a compute node.
  - **`askcos`** — ASKCOS v2, a **service** engine (POSTs to a running ASKCOS
    deployment, `PRECIS_ASKCOS_URL`).
- `max_steps=` — search depth (default 6). `requested_by=<todo>` — block that
  todo on the plan (see below).

**Content-addressed cache.** The plan is keyed on
`(target, engine, engine_version, stock, depth)`. A second identical `put`
returns a **cache hit with zero recompute**. Bumping the engine/model version
invalidates the key.

**Where it runs.** With a compute node configured (`PRECIS_CHEM_ROUTE_NODE`),
`put` mints a derived `retrosynth` **compute-lane job** (ADR 0044) parented on
the route — it runs off the request path and lands the plan on the route when
done (`get` to poll). With no node, the in-process `stub` runs inline (a real
engine there tells you to configure a node).

## get — read the route

```
get(kind='route')                      # list routes
get(kind='route', id='aspirin')        # the route graph (one line per step)
get(kind='route', id='aspirin', view='metrics')   # route descriptors
```

The default render is the **step graph**: `1. <product> ⇐ <precursors> [conf]
«template» ✔ in stock`, target first. A route is **solved** when every branch
reaches buyable (in-stock) leaves.

`view='metrics'` shows the **LinChemIn route descriptors** — `nr_steps`,
`longest_seq`, `nr_branches`, `branchedness`, `convergence`, `cdscore`, … — the
substrate for scoring/comparing routes (a view over stored numbers, never a
recompute). Empty for a `stub` route (no normalizer ran).

## Blocking a task on a plan

`requested_by=<todo_id>` wires the requesting todo to block on the job: a
`requested` link + a `derived_job_succeeded` auto-check, so the todo closes on
success and bubbles `child-failed` on failure (ADR 0044). Use when a task
genuinely needs the route before it can proceed.

## delete

```
delete(kind='route', id='aspirin')     # soft-retire
```

## One IR, many engines

Every engine normalizes to the *same* `route` IR via **LinChemIn** (the
Marker-analog for routes): the engine's native output → SynGraph → the route
graph you read. Container engines (aizynth) normalize in-image; service engines
(askcos) via a standalone normalizer. So "swap the engine, keep what you read"
is a fact — `get` renders identically whichever planner ran. Design:
`docs/design/chem-tools-integration.md`.

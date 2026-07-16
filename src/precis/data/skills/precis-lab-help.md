---
id: precis-lab-help
title: precis — the in-silico lab (agentic chem/bio research over the narrow verbs)
summary: the ChemCrow-analog playbook (ADR 0056 slice 6) — compose retrosynthesis (route), folding (protein), structure, and the literature into a research loop; precis has no chemistry agent framework, the planner/agent just drives the seven verbs, so this is the recipe layer, not a new tool
applies-to: put/get/search (kind='route'|'protein'|'structure'|'paper')
status: active
---

# precis-lab-help — chain the compute kinds toward a goal

precis's answer to a chemistry/biology **agent** (ChemCrow, ADR 0056 slice 6) is
*not* a separate framework with its own toolbox. The tools already exist as
**kinds** — `route` (retrosynthesis), `protein` (folding), `structure` (the 3D
atom graph), and `paper` (the grounded literature) — and the seven verbs already
drive them. This skill is the **composition layer**: the canonical recipes that
chain those verbs into a research loop, for an interactive agent *or* an
autonomous planner tick (`plan_tick`) working an `LLM:*` todo. Augmentation, not
foundation — read the per-kind skills for depth.

Each recipe is a **toolpath**: the minimal verb sequence for one goal. All the
heavy compute (a plan, a fold, a relax) runs **off the request path** on the
compute lane — you mint it and poll, never block. Everything is grounded in the
corpus, so a claim you make can be cited.

## Recipe: plan a synthesis for a target molecule

```
search(kind='paper', queries=['synthesis of <target>', 'total synthesis <scaffold>'],
       answers=['<a one-line HyDE answer>'], per_paper=2)   # prior art first
put(kind='route', id='<slug>', target='<SMILES>', engine='aizynth')   # mint the plan
get(kind='route', id='<slug>')                     # poll — the step graph
get(kind='route', id='<slug>', view='metrics')     # LinChemIn descriptors to score/compare
```

Read the prior art *before* proposing a route so you can favour a known
disconnection. Swap `engine='askcos'` for a second opinion — the IR is identical,
so `get` renders the same graph. A route is **solved** when every branch reaches
buyable leaves; an unsolved one is a hypothesis to refine (deeper `max_steps`, a
different engine). Depth: `precis-route-help`, `precis-search-help`.

## Recipe: fold + inspect a protein target

```
put(kind='protein', id='<slug>', sequence='<AA>', engine='alphafold3')   # mint the fold
get(kind='protein', id='<slug>')                        # poll — mean pLDDT / pTM
get(kind='protein', id='<slug>', view='structure')      # project into the 3D viewer
get(kind='structure', id='<slug>-fold')                 # probe it as an atom graph
```

Read the confidences before trusting the model: **mean pLDDT** is per-residue
local confidence (≥70 confident), **pTM** the global fold. A de-novo
(single-sequence) fold is a *hypothesis*, not ground truth — low confidence means
"needs an MSA-based engine or experimental backing", not "wrong". `view='structure'`
gives you the shared `/structure` 3D viewer + `find`/`dihedral`/coordination
probes. Depth: `precis-protein-help`, `precis-structure-help`.

## Recipe: a design loop (the ChemCrow spirit)

Given a goal — "a molecule that does X" / "a protein that binds Y" — chain the
kinds and iterate on the numbers, grounding each step:

1. **Frame it in the literature.** `search(kind='paper', …)` for the target class,
   known actives, prior routes. Cite what you lean on (`put(kind='citation', …)`).
2. **Propose + score.** Mint a `route` to the candidate molecule and/or a
   `protein` fold of the target; poll; read `view='metrics'` / mean pLDDT.
3. **Inspect the geometry.** `get(kind='protein', view='structure')` →
   `get(kind='structure', id='<slug>-fold')` and probe the active site.
4. **Decide + iterate.** Keep the high-confidence branch; re-mint with a deeper
   search / a different engine on the weak one. Every plan is content-addressed,
   so re-asking an identical question is a **zero-compute cache hit** — iterate
   freely.

The loop is bounded by real signals (route solved? pLDDT confident?), never by a
fixed step count — stop when the numbers converge or the corpus says you're done.

## Running it autonomously (planner tick)

An `LLM:*`-tagged todo whose goal is one of the above runs this playbook as a
`plan_tick` coroutine: each tick reads this skill, issues the next verb call,
records what it learned as a memory/comment, and either continues (`verdict:
continue`) or yields to the human (`ask-user:`) when a decision needs you (which
engine, whether a low-pLDDT fold is worth wet-lab time). The compute (route/fold)
lands off the tick via the compute lane, so a tick never blocks on the GPU — it
mints, then a later tick reads the result. Depth: `precis-dispatch-help`,
`precis-auto-tasks-help`.

## The discipline (same as the rest of precis)

- **Ground, then assert.** A route or a fold is a *model*; the corpus is the
  check. Prefer a cited claim over a confident one.
- **Read the numbers, not the vibe.** Route metrics + pLDDT/pTM are the truth of
  a compute result — a "plausible" route that isn't solved is unsolved.
- **Never block on compute.** Mint the job, poll the kind; the request path stays
  sub-second (ADR 0056 §6 content-addressed cache).
- **One IR, many engines.** Swapping `aizynth`↔`askcos` or (later) an MSA folder
  changes nothing you read — so try a second engine when the first is weak.

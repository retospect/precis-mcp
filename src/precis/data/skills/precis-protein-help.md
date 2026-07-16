---
id: precis-protein-help
title: precis — the protein kind (fold a sequence, read the structure you get)
summary: predict a protein structure from its amino-acid sequence with a swappable engine (stub/alphafold3) on the compute lane, content-addressed so a repeat is a zero-compute cache hit; read the fold as a confidence summary (mean pLDDT / pTM) or the raw mmCIF (view='cif') — never a synchronous GPU call
applies-to: get/put/delete (kind='protein')
status: active
---

# precis-protein-help — structure prediction the LLM can *read*

A `protein` is a **predicted structure for an amino-acid sequence** — the
sequence plus its folded mmCIF and confidence scores, normalized to one IR no
matter which predictor produced it (ADR 0056). It is the biology sibling of the
`route` kind and the keystone kinds (`structure`/`cad`/`pcb`): the LLM reads a
**summary + numbers** (and can pull the raw structure), never runs a GPU fold in
the request path. A `protein` is a plugin kind (precis-bio), **dark behind
`PRECIS_BIO_ENABLED`**.

Slug-addressed, three verbs (plus `tag`/`link`): `put` (fold / cache-hit),
`get` (list / summary / mmCIF), `delete` (soft-retire).

## put — fold a sequence

```
put(kind='protein', id='insulin-a', sequence='GIVEQCCTSICSLYQLENYCN', engine='alphafold3')
```

- `id=` — the protein slug (required). `sequence=` — the one-letter
  amino-acid sequence (required; validated — the 20 standard codes + `X`).
- `engine=` — the predictor (default `stub`):
  - **`stub`** — a deterministic, GPU-free toy predictor. No cluster, no deps:
    it exercises the substrate (mint → fold → cache) and returns a placeholder
    structure at a constant pLDDT. Use in tests / when no fold node is set.
  - **`alphafold3`** — AlphaFold3 **de-novo** (single-sequence, no MSA), a
    **container** engine on the GPU fold node.
- `seeds=[…]` — the AF3 model seeds (default `[1]`; part of the cache key).
  `requested_by=<todo>` — block that todo on the fold (see below).

**Content-addressed cache.** The fold is keyed on
`(sequence, engine, engine_version, mode, seeds)`. A second identical `put`
returns a **cache hit with zero recompute**. Bumping the engine/weights version
invalidates the key.

**Where it runs.** With a fold node configured (`PRECIS_FOLD_NODE`, plus
`PRECIS_FOLD_MODELS_DIR`), `put` mints a derived `fold` **compute-lane job**
(ADR 0044) parented on the protein — it runs off the request path (a fold is
~10 min on the GPU) and lands the structure on the protein when done (`get` to
poll). With no node, the in-process `stub` runs inline (a real engine there
tells you to configure a node).

## get — read the fold

```
get(kind='protein')                         # list proteins
get(kind='protein', id='insulin-a')         # the fold summary
get(kind='protein', id='insulin-a', view='cif')   # the raw mmCIF structure
```

The default render is the **fold summary**: residue count, **mean pLDDT** (0–100,
with a confidence band — very high ≥90 / confident ≥70 / low ≥50 / very low),
**pTM** / **ipTM**, ranking score, and the sequence. `view='cif'` returns the
predicted structure as mmCIF text (per-atom pLDDT in the B-factor column) for
download or a viewer.

**Reading confidence.** Mean pLDDT is per-residue local confidence; pTM is the
global fold confidence. De-novo (single-sequence) folds are **less accurate than
MSA-based** ones — treat a low-pLDDT de-novo model as a hypothesis, not ground
truth (MSA mode is a later engine).

## Blocking a task on a fold

`requested_by=<todo_id>` wires the requesting todo to block on the job: a
`requested` link + a `derived_job_succeeded` auto-check, so the todo closes on
success and bubbles `child-failed` on failure (ADR 0044). Use when a task
genuinely needs the structure before it can proceed.

## delete

```
delete(kind='protein', id='insulin-a')      # soft-retire
```

## One IR, many predictors

Every engine normalizes to the *same* `protein` IR: sequence + mmCIF + the
scalar confidences. Swap AlphaFold3 for ColabFold (MSA) and `get` renders
identically. The heavy predictor runs in a container on the GPU node — the
weights are mounted, never baked (ADR 0056 §5) — so the always-on request path
carries no bio dependencies. Design: `docs/design/chem-tools-integration.md`.

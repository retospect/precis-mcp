---
id: precis-paper-tag-axes
title: precis — paper auto-tagging taxonomy
status: draft
tier: 2
floor: any
applies-to: tag (kind='paper'), search (tags=, kind='paper')
last-updated: 2026-05-07
---

# precis-paper-tag-axes — paper auto-tagging taxonomy

A small fleet of LLM classifiers tag every `paper` ref along a fixed
set of **axes**. Each axis has a closed vocabulary and an
applicability gate (which papers it applies to). Tags are written as
lowercase open tags so they're queryable through the normal
`search(... tags=[...])` path.

```python
search(kind='paper', q='transport',
       tags=['scale:nano', 'studytype:experimental'])
```

This skill documents the **vocabulary**. The runner, prompts, and
gating live in `src/precis/data/axes/*.yaml`.

## Per-paper state — `meta.processing`

Classification state is stored on `refs.meta.processing` (JSONB):

```json
{
  "processing": {
    "domain":    {"v": 1, "value": "materials",    "confidence": 0.95,
                  "model": "qwen3.5:9b", "at": "2026-05-07T..."},
    "scale":     {"v": 2, "value": "nano",         "confidence": 0.92, ...},
    "studytype": {"v": 1, "value": "experimental", "confidence": 0.88, ...}
  }
}
```

The `v` field is the axis schema version. The classifier runner
skips any axis already at the current version. To re-classify, bump
the axis YAML's `version:` and re-run.

## The axes

### `domain:` — broad subject area  *(applies to all papers)*

Run first; gates every other axis.

| value         | when |
|---------------|------|
| `chemistry`   | chem, materials chem, electrochem |
| `physics`     | condensed matter, optics, atomic |
| `bio`         | biology, medicine, neuroscience, psychology |
| `materials`   | materials science, nanomaterials |
| `eng`         | engineering, devices, systems |
| `other`       | clearly outside the four above |
| `unknown`     | LLM cannot tell from title + abstract |

### `scale:` — spatial scale of the *thing studied*

Applies when `domain ∈ {chemistry, physics, materials, eng}`.

| value     | criterion |
|-----------|-----------|
| `atomic`  | individual atoms, single bonds, single defects |
| `nano`    | < 100 nm typical dimension; nanotubes, dots, 2D mono- to few-layer |
| `meso`    | 0.1–10 µm typical dimension |
| `micro`   | 10 µm – 1 mm |
| `bulk`    | > 1 mm; macroscopic samples, thin films treated as continuum |
| `multi`   | the paper deliberately spans more than one scale |
| `unknown` | can't tell from title + abstract |
| `n-a`     | doesn't have a spatial scale (pure theory, methodology, review) |

### `dim:` — geometric dimensionality of the *system*

Applies when `domain ∈ {chemistry, physics, materials, eng}`.

| value | criterion |
|-------|-----------|
| `0d`  | quantum dots, fullerenes, nanoparticles |
| `1d`  | nanotubes, nanowires, chains |
| `2d`  | graphene, MXenes, monolayers |
| `3d`  | bulk crystals, MOFs, dense networks |
| `mixed` | hybrid 1d-on-2d, 0d-on-3d, etc. |
| `n-a` | no geometric structure (pure method, theory, review) |

### `transport:` — geometry of conductivity / current path

Applies when `domain ∈ {physics, materials, eng}` AND
`property:` involves `electrical`. Optional otherwise.

| value           | criterion |
|-----------------|-----------|
| `point-contact` | single tip, single junction (STM, break-junction) |
| `few-atom`      | ballistic over ≤ ~10 atoms |
| `thin-film`     | planar film, 2D sheet conduction |
| `bulk-3d`       | 3D current path, isotropic or anisotropic bulk |
| `interfacial`   | conduction localised at an interface or grain boundary |
| `n-a`           | paper doesn't measure or compute electrical transport |

### `studytype:` — what kind of study  *(applies to all papers)*

| value               | criterion |
|---------------------|-----------|
| `experimental`      | wet-lab measurements on real samples |
| `synthesis`         | the contribution is making the material |
| `characterization`  | the contribution is measuring known materials |
| `theory-dft`        | DFT or ab-initio calculations |
| `theory-md`         | molecular dynamics |
| `simulation-other`  | continuum, FEA, Monte Carlo, kinetics |
| `review`            | review article, perspective, opinion |
| `mixed`             | substantial contributions in two or more above |

### `material:` — primary material class

Applies when `domain ∈ {chemistry, materials, eng}`.

| value          | examples |
|----------------|----------|
| `cnt`          | carbon nanotubes (SWCNT, MWCNT, CNF) |
| `graphene`     | graphene, GO, rGO |
| `nanobud`      | carbon nanobuds (graphene/CNT + fullerene) |
| `fullerene`    | C60, C70, endohedrals |
| `mof`          | metal-organic frameworks |
| `metal-oxide`  | TiO2, ZnO, Cu2O, perovskite oxides |
| `polymer`      | conducting and structural polymers |
| `other`        | something specific but not in the list |
| `unknown`      | can't tell |
| `n-a`          | no specific material (pure theory, method, review) |

### `property:` — primary property under study

Applies when `domain ∈ {chemistry, physics, materials, eng}`.

| value         | criterion |
|---------------|-----------|
| `electrical`  | conductivity, resistance, transport, junction I-V |
| `mechanical`  | strength, stiffness, fracture, fatigue |
| `thermal`     | thermal conductivity, capacity, expansion |
| `optical`     | absorption, emission, plasmonics, Raman |
| `chemical`    | catalysis, adsorption, reactivity |
| `magnetic`    | magnetism, spintronics |
| `multi`       | the paper investigates two or more in depth |
| `unknown`     | can't tell |
| `n-a`         | no measured property (pure synthesis, review, method) |

## Prefilter — `journal_domains.yaml`

Before any LLM call, the runner consults
`src/precis/data/axes/journal_domains.yaml` for a
`journal-pattern → likely-domain` mapping. A confident match skips
the `domain:` LLM call entirely. Anything unmapped falls through to
the LLM.

## Re-running

- **Per axis**: bump `version:` in `axes/<axis>.yaml`. Runner
  reclassifies refs whose `meta.processing.<axis>.v` is below.
- **Per paper**: pass `--rerun --slug=<slug>` to
  `precis jobs classify-papers`.
- **Per axis on subset**: combine axis filter + tag filter.

## Validation — gold set

Before bulk runs, every axis must score ≥ 85% on the
30-paper hand-labeled gold set in
`pips/packages/precis-mcp/scripts/classify/gold_set/`. The eval
harness (`scripts/classify/eval-classifier`) prints per-axis
accuracy and confusion matrices.

## Why open lowercase tags

The matrix in `precis-tags` shows that `paper` only allows the
closed `SRC:` and `CACHE:` axes. Adding more closed prefixes for
classification would require code changes in `store/types.py` for
each new axis. Open tags (`scale:nano`, `dim:1d`) accumulate freely
on every kind, work with `search(tags=[...])`, and let us evolve
the taxonomy without migrations.

## See also

- `precis-paper-help` — find/read/cite papers
- `precis-tags` — tag vocabulary, closed vs open
- `axes/*.yaml` — machine-readable axis definitions
- `scripts/classify/` — cluster, sample-gold, eval-classifier, classify-papers

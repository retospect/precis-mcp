---
id: precis-paper-tag-axes
title: precis — paper auto-tagging taxonomy
applies-to: tag (kind='paper'), search (tags=, kind='paper')
status: active
---

# precis-paper-tag-axes — paper auto-tagging taxonomy

Ingest auto-tags every paper along a fixed set of lowercase **axes**
(`domain:`, `scale:`, `dim:`, `transport:`, `studytype:`, `material:`,
`property:`). Filter searches by these axes via `tags=[...]`.

For closed UPPERCASE axes (`SRC:`, `CACHE:`) and the open-tag rules,
see `precis-tags`. Paper allows `SRC:` and `CACHE:` only.

## Filter papers by an axis value
## Narrow search to papers tagged scale:nano
## Combine topic search with auto-tag filters

```python
search(kind='paper', q='transport', tags=['scale:nano', 'studytype:experimental'])
search(kind='paper', q='photocatalysis', tags=['domain:materials', 'material:metal-oxide'])
search(kind='paper', tags=['studytype:review'])               # tag-only, no q=
```

Axis tags AND together. Combine freely with `topic:`, `SRC:`, `CACHE:`.

## What axes get auto-applied to a paper
## Which tags does ingest write
## Auto-tagging vocabulary for papers

Seven axes. `domain:` runs first; the rest gate on its value.

### `domain:` — broad subject area  *(all papers)*

| value | when |
|---|---|
| `chemistry` | chem, materials chem, electrochem |
| `physics` | condensed matter, optics, atomic |
| `bio` | biology, medicine, neuroscience, psychology |
| `materials` | materials science, nanomaterials |
| `eng` | engineering, devices, systems |
| `other` | clearly outside the four above |
| `unknown` | can't tell from title + abstract |

### `scale:` — spatial scale of the thing studied

Gates on `domain ∈ {chemistry, physics, materials, eng}`.

| value | criterion |
|---|---|
| `atomic` | individual atoms, single bonds, single defects |
| `nano` | < 100 nm; nanotubes, dots, 2D mono- to few-layer |
| `meso` | 0.1–10 µm |
| `micro` | 10 µm – 1 mm |
| `bulk` | > 1 mm; macroscopic samples, thin films as continuum |
| `multi` | deliberately spans more than one scale |
| `unknown` | can't tell |
| `n-a` | no spatial scale (pure theory, method, review) |

### `dim:` — geometric dimensionality

Gates on `domain ∈ {chemistry, physics, materials, eng}`.

| value | criterion |
|---|---|
| `0d` | quantum dots, fullerenes, nanoparticles |
| `1d` | nanotubes, nanowires, chains |
| `2d` | graphene, MXenes, monolayers |
| `3d` | bulk crystals, MOFs, dense networks |
| `mixed` | hybrid 1d-on-2d, 0d-on-3d, etc. |
| `n-a` | no geometric structure |

### `transport:` — geometry of conductivity / current path

Gates on `domain ∈ {physics, materials, eng}` AND `property:electrical`.
Optional otherwise.

| value | criterion |
|---|---|
| `point-contact` | single tip, single junction (STM, break-junction) |
| `few-atom` | ballistic over ≤ ~10 atoms |
| `thin-film` | planar film, 2D sheet conduction |
| `bulk-3d` | 3D current path |
| `interfacial` | conduction at an interface or grain boundary |
| `n-a` | no electrical transport measured |

### `studytype:` — what kind of study  *(all papers)*

| value | criterion |
|---|---|
| `experimental` | wet-lab measurements on real samples |
| `synthesis` | contribution is making the material |
| `characterization` | contribution is measuring known materials |
| `theory-dft` | DFT or ab-initio calculations |
| `theory-md` | molecular dynamics |
| `simulation-other` | continuum, FEA, Monte Carlo, kinetics |
| `review` | review, perspective, opinion |
| `mixed` | substantial contributions in two or more above |

### `material:` — primary material class

Gates on `domain ∈ {chemistry, materials, eng}`.

| value | examples |
|---|---|
| `cnt` | carbon nanotubes (SWCNT, MWCNT, CNF) |
| `graphene` | graphene, GO, rGO |
| `nanobud` | carbon nanobuds |
| `fullerene` | C60, C70, endohedrals |
| `mof` | metal-organic frameworks |
| `metal-oxide` | TiO2, ZnO, Cu2O, perovskite oxides |
| `polymer` | conducting and structural polymers |
| `other` | specific but not in the list |
| `unknown` | can't tell |
| `n-a` | no specific material |

### `property:` — primary property under study

Gates on `domain ∈ {chemistry, physics, materials, eng}`.

| value | criterion |
|---|---|
| `electrical` | conductivity, resistance, transport, I-V |
| `mechanical` | strength, stiffness, fracture, fatigue |
| `thermal` | thermal conductivity, capacity, expansion |
| `optical` | absorption, emission, plasmonics, Raman |
| `chemical` | catalysis, adsorption, reactivity |
| `magnetic` | magnetism, spintronics |
| `multi` | two or more in depth |
| `unknown` | can't tell |
| `n-a` | no measured property |

## Conventions for topic slugs
## How should I shape a topic: tag on a paper?

`topic:` is open — coin freely. Use lowercase, hyphen-separated, no
spaces or unicode:

```python
tag(kind='paper', id='<slug>', add=[
    'topic:co2-capture',           # good
    'topic:noxrr',                 # good (compact acronym)
    'topic:z-scheme-photocatalysis',
])
```

Reuse existing slugs when one fits — `search(kind='paper', tags=['topic:<guess>'])`
to check before coining. Auto-tag axes (`scale:`, `dim:`, …) are
closed vocabularies above; don't mint new values inside those
prefixes.

## See also

```python
get(kind='skill', id='precis-tags')             # closed axes (SRC, CACHE), validation
get(kind='skill', id='precis-paper-help')       # find, read, cite papers
get(kind='skill', id='precis-search-help')      # tags= filter mechanics
get(kind='skill', id='precis-tag-help')         # the tag verb itself
```

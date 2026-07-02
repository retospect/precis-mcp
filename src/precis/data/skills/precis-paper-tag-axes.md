---
id: precis-paper-tag-axes
title: precis — paper auto-tagging taxonomy
summary: paper auto-tag taxonomy — domain, scale, dim, transport, studytype, material, property axes
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

### `scale:` — characteristic length of the thing studied

Gates on `domain ∈ {chemistry, physics, bio, materials, eng}`. Decade
buckets — a value names the decade floor (half-open range). The
length is the *phenomenon's* scale: MOF adsorption → the pore, not
the crystal; thin-film transport → the film thickness.

| value | criterion |
|---|---|
| `atomic` | < 1 nm — atoms, bonds, defects, small molecules |
| `1nm` | 1–10 nm — pores, macromolecules, dots, few-layer 2D |
| `10nm` | 10–100 nm — nanoparticles, thin films, nanowires |
| `100nm` | 100 nm – 1 µm |
| `1um` | 1–10 µm — crystallites, cells, MEMS features |
| `10um` | 10–100 µm |
| `100um` | 100 µm – 1 mm |
| `mm` | 1 mm – 1 m — flow cells, reactors, macroscopic samples |
| `m` | 1 m – 1 km — pilot plants, field scale |
| `km` | ≥ 1 km terrestrial — atmospheric, oceanic, geological |
| `astro` | beyond Earth — planetary, stellar, galactic |
| `multi` | contrasting scales IS the finding |
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

### `studytype:` — epistemic mode: how the paper knows  *(all papers)*

| value | criterion |
|---|---|
| `conceptual` | theory / analytical derivation; no simulation, no experiment |
| `computational` | simulation dominates (DFT/MD/MC/FEA/ML — flavor is chunk-level `method:`) |
| `experimental-ensemble` | measurement averaged over many entities (bulk, films, solutions) |
| `experimental-single-entity` | one entity resolved at a time (break junction, single-molecule, single-particle) |
| `synthesis` | making the thing IS the contribution |
| `review` | review, perspective, opinion |
| `mixed` | genuinely co-equal modes |
| `unknown` | can't tell from title + abstract |

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

Address the paper by its handle (`pa<id>`); the slug still resolves.
The `topic:` *values* stay slugs — they are content tags, not an
address:

```python
tag(kind='paper', id='pa812', add=[      # handle (id='<slug>' still resolves)
    'topic:co2-capture',           # good
    'topic:noxrr',                 # good (compact acronym)
    'topic:z-scheme-photocatalysis',
])
```

Reuse existing slugs when one fits — `search(kind='paper', tags=['topic:<guess>'])`
to check before coining. Auto-tag axes (`scale:`, `dim:`, …) are
closed vocabularies above; don't mint new values inside those
prefixes.

## Chunk-level axes (ADR 0047)

A second family of axes tags individual **chunks**, not papers, for
facets that vary within a document. First axis: `role:` — the
rhetorical role of a chunk (`axes/role.yaml`). Single-select:

`motivation` (why this work) · `related-work` (recaps other papers /
general background — excludable) · `method` (what this paper did,
procedurally) · `result` (this paper's OWN findings — the only
legitimate citation-quote target) · `interpretation` (what results
mean) · `limitation` · `future-work` · `data` (tables/numeric
listings) · `boilerplate` (publisher furniture — excludable) ·
`unknown` / `n-a` (envelope-only, never written as tags).

Filter chunks the same way (`tags=` matches chunk-level tags too):

```python
search(kind='paper', q='co2 adsorption capacity', tags=['role:result'])
search(kind='paper', q='gcmc setup', tags=['role:method'])
```

Second chunk axis: `open-question:` (`axes/open-question.yaml`) — a
recall-biased binary flag for experiment planning: does the chunk
name something *specific* not yet done (future direction, open
question, untried condition, acknowledged failure, negative result)?
Cross-cutting by design — leads live in `future-work`, `limitation`,
`result`, and even `motivation` chunks:

```python
search(kind='paper', q='mof water stability', tags=['open-question:yes'])
```

The load-bearing boundary is `result` vs `related-work`: a
`\citequote` must land on a paper's own contribution, never on its
summary of someone else's. Applied by the background `chunk_tag`
pass; values are closed vocabularies — don't mint inside `role:` or
`open-question:`.

## See also

```python
get(kind='skill', id='precis-tags')             # closed axes (SRC, CACHE), validation
get(kind='skill', id='precis-paper-help')       # find, read, cite papers
get(kind='skill', id='precis-search-help')      # tags= filter mechanics
get(kind='skill', id='precis-tag-help')         # the tag verb itself
```

---
id: precis-structure-import-help
family: science
title: precis — importing structures (CIF text or URL, QMOF, CoRE MOF, Catalysis-Hub)
summary: put a CIF (text or URL) as a structure design, or pull one record from an external database (QMOF, CoRE MOF, Catalysis-Hub) as a cited, read-only design with its method fingerprint; caps, refusals, what an imported design can and cannot do
answers:
  - how do I import a CIF or a MOF (QMOF, CoRE MOF) as a structure?
  - how do I import a structure from an external catalyst database?
  - why is my imported structure read-only?
applies-to: put/get (kind='structure')
status: active
tags: design, verbs
kinds: structure
---

# precis-structure-import-help — structures from files and databases

The rest of the structure kind (ops, probes, relax) is `precis-structure-help`.

## Import a CIF — `put(id=<slug>, args={'cif': …})`

```python
put(kind="structure", id="abavij", args={"cif": "<CIF text>"})
put(kind="structure", id="abavij", args={"cif_url": "https://…/ABAVIJ.cif"})
```

- The CIF's cell and symmetry-expanded atoms become an ordinary, editable
  design (no bonds yet; no external run). A bare CIF in `text=` works too.
- Caps: 2 MiB of text and 5,000 atoms after symmetry expansion; a bigger
  cell, a body that is not CIF, or a CIF with no `_atom_site` loop is
  refused with a next step. `cif_url` must be public http(s).
- Name the source to import a database record instead:
  `put(kind='structure', args={'cif': …, 'source': 'core-mof', 'config_id':
  'ABAVIJ_clean'})` lands read-only under the import slug (no `id=`), with
  an external run carrying the source's method fingerprint; importing the
  same `config_id` again reuses the design.

## Import from an external catalyst DB

`get(kind='structure', args={'source': 'catalysis-hub', ...})` hydrates a
real, DFT-relaxed config from an external library into an ordinary
`structure` design — a "quest worker pokes around and pulls a real
substrate" surface, not a separate API.

```python
get(
    kind="structure",
    args={"surface_composition": "Pd", "facet": "111", "source": "catalysis-hub"},
)  # a filtered fetch — hydrates every match, renders a summary table if >1
get(
    kind="structure",
    args={"config_id": "12345", "source": "catalysis-hub"},
)  # exact config_id: a network-free cache hit if already imported
```

- **First touch hydrates, forever after is a cache hit.** The fetch → adapter
  → `store.structure_import` write path is idempotent on `(dataset,
  config_id)` (`ref_identifiers`); a repeat `config_id=` lookup never refetches.
  A broad `surface_composition=`/`facet=`/`q=` filter (no `config_id=`) always
  refetches — there's no way to know in advance whether new configs match.
- **Only `catalysis-hub` has a `get` fetch layer wired today** (its network
  client, httpx, is a core dep); an unregistered source raises `BadInput`
  naming the known ones. A broken venv missing httpx returns
  `Unsupported` with a reinstall hint, never a crash. QMOF and CoRE MOF
  records come in through the CIF `put` with `source=` above.
- **The `catalysis-hub` *live* fetch is dark** (verified 2026-07-24): SUNCAT
  gated all public access — the GraphQL API 401s without an `X-API-Key` and
  the old public Postgres password is rotated. The on-demand `source=` path is
  code-complete but needs a SUNCAT credential to reach the network.
- **Keyless ingress — mine a local cathub `.db`.** A cathub `.db` is a
  self-contained, credential-free package (relational reactions + embedded ASE
  structures + citation). The bulk import is operator-run, not a verb — ask a
  human to run it. Each reaction's product adsorbate config lands as an
  ordinary `structure` (its external run carries the adsorption energy +
  method fingerprint), searchable as usual.
- **Imported designs are read-only.** They carry `provenance:external` on
  their `struct_runs` row; `edit` refuses ("derive a variant instead") —
  branch off one with `derive(id=<imported-slug>, to=<new-slug>, ops=[...])`.
  `view='runs'` labels each row's provenance + method fingerprint
  (functional/cutoff/spin/dataset_doi/facet/...).
- **Energies are only comparable within one method.** An external run's DFT
  functional/cutoff differs from a computed rung's model — subtracting across
  them is a category error, not a real ΔE, so an energy-delta surface must
  check both runs share a method fingerprint before comparing (geometry/graph
  reads like `diff` stay method-agnostic).


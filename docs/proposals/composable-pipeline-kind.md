---
status: draft
title: Composable pipeline kind for chained chem/text point-operations
model: opus
---

# Composable pipeline kind for chained chem/text point-operations

> Migrated from gripe 43939 (feature-request, pipeline, cheminformatics).
> Captures the gripe's design options as-is; no new design invented here.

## Motivation / why

A growing set of point operations exists (extract chem names from text,
classify compound vs family, fetch SMILES, look up papers by entity) with no
way to chain them without bespoke glue. A pipeline model
(`extract_chem | classify | fetch_smiles | lookup_papers`) would make ad-hoc
cheminformatics workflows trivial to compose and reuse.

Candidate operations, by tier (already-existing or plausible additions):

- **Text/NER**: `extract_chem_names`, `extract_doi`/`extract_identifiers`,
  `extract_quantities`
- **Classification**: `classify_chem` (specific compound vs family — e.g.
  "MOF" → family, "UiO-66" → specific), `resolve_synonym` (normalize name
  variants)
- **Lookup/fetch**: `fetch_smiles` (name → canonical SMILES via PubChem),
  `fetch_inchi`/`fetch_inchikey`, `fetch_formula`/`fetch_mw`, `fetch_cas`,
  `fetch_structure_img`
- **Library integration**: `search_papers` (entity → corpus papers),
  `search_chunks` (entity → chunk hits), `get_paper_context` (chunk id →
  surrounding paragraph)
- **Transform**: `canonicalize_smiles`, `strip_salts`, `tautomer_parent`,
  `smiles_to_inchi`/`inchi_to_smiles`
- **Property (external)**: `fetch_logp`/`fetch_pka`/`fetch_solubility`,
  `fetch_toxicity_flags`, `fetch_synthesis_routes`
- **Chem-structure search**: `substructure_search` (SMILES query → corpus
  matches), `similarity_search` (Tanimoto cutoff → nearest neighbours)

## In scope

**Option A** (start here, per the gripe's design log): `kind='pipeline'` +
a `run` verb.

- Named pipelines as first-class precis objects:
  `put(kind='pipeline', id='smiles-from-text', steps=[...])`.
- `run(kind='pipeline', id=..., input=...)` executes a stored pipeline — a
  new top-level `run` verb (or `get` with `?execute=true`).
- Built-in ops hardcoded initially (not yet first-class objects — see Option
  B below, deferred).

## Explicitly NOT in scope

- **Option C — inline `pipe=` param on existing verbs.** No new objects, no
  reuse, pollutes unrelated verbs. Dead end per the gripe's own assessment;
  not to be built.
- **Option B — `kind='op'` as a first-class stored/versioned object**, with
  defined I/O contracts, pipelines referencing op IDs by reference, and ops
  testable in isolation. This is the gripe's stated end state, but
  explicitly a later graduation — migrate to it once op shapes stabilize
  under Option A, not before.
- Building out the individual point operations themselves (extract/classify/
  fetch/transform/property/structure-search) — those are chemistry
  tool-pack plugin tools under ADR 0056, which already owns them. This
  proposal is the chaining/composition layer over ops that exist or land
  there, not the ops.

## Acceptance criteria

- A named pipeline object can be created (`put(kind='pipeline', ...)`)
  chaining N built-in ops with defined step order.
- `run` executes a stored pipeline by id against an input and returns the
  composed result (each step's output feeding the next step's input).
- The pipeline object is reusable: the same stored pipeline can be run
  multiple times against different inputs without redefinition.

## Target + blast radius

- **New:** a `kind='pipeline'` kind handler; a `run` verb (or `get`
  `?execute=true` execution path).
- **Depends on:** the chem tool-pack ops from ADR 0056
  (`docs/decisions/0056-chemistry-tool-packs-plugin-route-kind.md`) as the
  step vocabulary — this pipeline layer chains ops that ADR 0056 defines and
  owns individually; it does not redefine them.
- **Not touched:** ingest, embeddings, existing verb surfaces (Option C —
  inline params on existing verbs — is explicitly rejected, so no existing
  verb signature changes).

## Open questions / decisions log

- **Unix-CLI-primitives vs kind-based**: the gripe raised building on unix
  primitives (stdin/stdout, jq, xargs, each op a CLI tool — zero new infra,
  but messy for rich objects like images/structured data) as an alternative
  to a precis-native pipeline kind. Not resolved in the gripe; Option A
  (kind-based) was the stated starting recommendation, but the CLI-primitive
  route was never formally ruled out.
- **Option A → Option B migration trigger**: what signals that op shapes
  have "stabilized" enough to promote hardcoded built-in ops to first-class
  `kind='op'` objects. Not specified in the gripe.
- **Rich-object I/O contracts**: how a pipeline step passes non-scalar
  outputs (structure images, structured property bundles) to the next step
  — relevant to both the unix-primitives objection and the eventual Option B
  I/O-contract design. Unresolved.

---
status: draft
title: attached-models layer — multi-fidelity analysis results with validity scope and loud staleness
prio: high
model: opus
---

# Attached-models layer (axis 3 — model fidelity)

Design session 2026-09-04 (Reto + agent), imperative-plotting-hare worktree.
Reto's ask: "an (ideally) mostly reusable model set with dft/mlpotential or
multiphysics, at Angstrom to meter scale" + "a way for code to flag issues
to the ai." Best-practice grounding: `perplexity-research:317035` — the
*Scoped Simulation Artefact Pattern* (SPDM): every analysis result is a
first-class artefact node with fidelity, boundary conditions, validity
scope, provenance chain, and staleness relations that fire on upstream
change.

## The shape

A model result is a **`finding`/`estimate` ref linked `analyzed-by` to the
block it describes** (`design-graph-relations.md`), carrying:

- **fidelity tier + engine** — analytic probe / beam-shell FEA / full
  multiphysics; MLP / semi-empirical / DFT. The tier ladder cad already
  climbs (tier-1 analytic probes exist today).
- **validity scope** — the load-bearing field: which ports/interfaces the
  result assumed, and what it assumed across them (terminating H's, fixed
  constraint, applied torque, temperature range). Carrier: **`scope=`** on
  the finding put path (a structured dict → `meta.scope`, already part of
  the identity hash, already card-rendered as `key=value` pairs, already
  linted). *Correction 2026-09-05: this doc previously said `conditions=`
  — that kwarg exists only on the material/component sourced-value path.*
- **provenance chain** — which geometry/structure version, which mesh or
  basis settings, which engine version. Cite durable anchors, not blobs.

## Staleness is event-driven and LOUD

When anything inside the declared scope changes — the block itself, or the
far side of a scoped interface — the result flips stale and **code files a
gripe/alert on the design**. That is the "code flags issues to the AI"
channel, and it is the existing gripe machinery plus a watcher on
ref/chunk events, not a new subsystem. Three tiers, two of which exist:
put-time lint (cad_propose dry_run), read-time probes, and (new) the
async staleness watcher. The se-kind rule applies doubly here:
declared-but-unchecked physics is worse than absent physics — never render
a stale number without its flag.

**Exports are scoped artefacts too.** A STEP/STL exported yesterday has no
idea the hinge module changed today (the CAD-world "underived hole series"
failure). Export events should record the source version so the same
watcher can flag drifted exports.

## The port type IS the validity boundary

The unifying insight (both scales): a well-typed interface is precisely the
promise that the far side is exchangeable. Model reuse is valid exactly to
the degree the port type captures the coupling. If a fragment's DFT energy
shifts when connected, the port type was too weak (bare "single bond" when
charge environment matters) — that discovery feeds the se-kind annotation
registry as a new *checked* key, entering with its consumer. Reusability
is not a separate system; it is a measure of interface-type honesty.

## ML potentials: flag the MODEL, not just the result

The report's sharpest chemistry finding: MLPs are interpolators (~1
meV/atom inside the training domain) and poor extrapolators — physically
meaningless outside it. So the *potential itself* is a ref (the `llm`
catalog pattern, but for physics engines) carrying its training-domain
coverage (compositions, bonding environments); a use outside that domain is
flagged **extrapolative before any result exists**, suggesting DFT.
Mech mirror: a beam model reused to justify local stress at a fastener hole
is the same failure — scope on the model class, not only the run.

## v1 implementation notes (survey 2026-09-05, verified against the tree)

- **Version anchor first — cad_save records nothing today.** No rev
  counter, `refs.updated_at` not bumped (no trigger; the re-save UPDATE
  omits it — arguably a standalone bug), no `ref_events` row. v1
  prerequisite, all inside `cad_save`'s existing tx: bump
  `updated_at`, a monotonic `meta.rev`, and
  `append_event(source="cad", event="saved", payload={"rev": …})`.
- **The pin rides `links.meta`** (jsonb, precedent: 0095's `{qty, ref}`):
  `add_link(…, relation="analyzed-by", meta={"rev": N, "at": iso},
  merge_meta=True)` — re-analysis updates the pin. Staleness check is
  then one SQL comparison (`links.meta->>'rev'` vs `refs.meta->>'rev'`),
  no event-scan needed; the `ref_events` row stays as audit trail + the
  export-drift case.
- **Watcher = one `Condition` row** in `src/precis/workers/conditions.py`
  (probe over links⋈refs), evaluated hourly by health_digest — inherits
  the alert-sync → router → gripe machinery (marker-line dedupe,
  fingerprint e.g. `cad:<slug>/analysis:<fi>`, flood cap, auto-close on
  refresh) for free. Only fall back to a standalone scanner
  (`draft_refresh_scan.py` shape) if the hourly cadence doesn't fit.
- **Do NOT use `STATUS:stale` on findings** — `STATUS:` is a closed
  one-value axis (tracing/acquiring/established/refuted/canonical); stale
  would clobber the lifecycle value. Staleness is derivable from the pin;
  if a tag is wanted for search, use a separate flag axis.
- **Relation minting**: `analyzed-by` (+ inverse `analysis-of`) enters
  with this item — see `design-graph-relations.md` for the per-consumer
  revision. Migration pattern: `0095_component_contains.sql`; 0152 is
  taken (se component_geometry_specs), use the next free number.
- Links are chunk-addressable (`src_chunk_id`/`dst_chunk_id`), so an
  attachment can target one cad node — but cad node chunk ids are rebuilt
  every save, so v1 pins **ref-level only**.

## Sequencing

1. Not before `design-graph-relations.md` (needs `analyzed-by`).
2. v1 = schema + manual attach + staleness watcher + card rendering
   ("stress 42 MPa (FEA, STALE since <event>)"). No engine integration.
3. Engines arrive as job_types per tier (multiphysics; DFT/MLP rides the
   existing compute-lane on castor/pollux). MTBF/reliability: parked — see
   `margin-budget-tree.md`.
4. **Hard rule (Reto + agent, 2026-09-04): do not ship attachment storage
   before the staleness design.** Attached-but-silently-stale numbers are
   worse than no numbers.

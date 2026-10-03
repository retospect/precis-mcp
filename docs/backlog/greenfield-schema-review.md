---
status: draft
title: greenfield schema review — what the DB would look like designed today, and the migration path there
pillar: platform
prio: normal
model: opus
---

# greenfield schema review

Reto, 2026-10-02 (knowledge-mesh-6): "it's probably time to file a
backlog item for a db refactoring considering greenfield." File only;
nothing is decided.

## Motivation / why

The schema grew kind by kind: `refs` + `chunks` + `links` as the mesh,
then side tables per kind with their own value stores
(`material_values`, `component_spec_values`, `rxn_values`), a catalog
table outside refs (`parts`, swapped daily), design internals that are
not rows (`pcb_instances`, se elements by name), and `measures` planned
to unify the value stores. Each step was forward-only and local. The
mesh goal (every part, item and design a node) and the measures fold-in
both ask the same question: if we designed the schema today, knowing
every kind we have, what would it be?

## In scope

1. An inventory: every table, which kind or subsystem owns it, row
   counts on prod, and which are refs-backed.
2. A target design: the minimal set of tables a greenfield schema would
   have for refs, chunks, links, values/measures, catalogs, design
   internals, jobs/telemetry, and why each one exists.
3. A gap list from today's schema to that target, each gap with its
   migration path (forward-only, ADR 0005) and cost.
4. A recommendation: which gaps are worth closing and in what order,
   versus living with.

## Explicitly NOT in scope

- Doing any refactor; this is a review that ends in a document and a
  ranked list.
- Replacing Postgres, or a graph database (rejected in knowledge-mesh
  with a stated revisit trigger).

## Acceptance criteria

1. A design note with the inventory, the target, the gap list and the
   recommendation, reviewed by Reto.
2. Each recommended gap filed as its own backlog item with a migration
   sketch.

## Target + blast radius

Read-only: `docs/reference/schema.md`, the migrations, prod row counts
(`scripts/prod-psql`, read-only).

## Open questions / decisions log

- **[decided 2026-10-02, Reto knowledge-mesh-6]** File it; review only.
- **[gap, 2026-10-03, from local-mesh-upkeep §2b]** Revision history
  sits in two tables: `chunk_events` (chunks; drives the embed/summary
  cascade) and `revisions` (refs and links). A greenfield schema has one
  log over all targets, plus one `reviews` ledger. The path there: the
  cascade reads a view, then `chunk_events` folds in.

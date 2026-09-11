---
status: draft
title: freedom to operate — defensive-publication SOP (Zenodo, TDCommons, IP.com) with xrefs
prio: medium
---

# Freedom to operate — defensive publication as SOP

Reto, 2026-09-11 (gr334824). Make dated public disclosure a standing part
of the ship workflow so nobody can later patent-block capabilities we
built. Three legs plus a closed loop.

## Leg 1 — Zenodo repo sync, regularly

Archival DOI-stamped snapshots of the (already public) repo. GitHub
history alone is weak prior art — mutable, no archival guarantee; Zenodo
gives a DOI, an immutable timestamp, and CERN-backed permanence, with
native GitHub-release integration (one-time webhook + cutting releases on
a cadence, or an API-driven job in the routine lane). Decide: release
cadence (monthly? per major capability?) and the licence posture on the
deposits.

## Leg 2 — tech-report disclosures, per shipped capability

When a capability-class feature ships (the kind a patent could block:
m−s stability classifier surface, AM-filter SIMP pipeline, blocktree
state machinery), publish a short technical-disclosure report. The design
docs already contain the prose — a post-`/go` step or routine drafts the
disclosure from the shipped backlog/design doc **for review; never
auto-publish** (outward-facing, irreversible → human sign-off gate).

Venue is per-artifact, by blocking risk:

- **TDCommons** — free, open-access, Creative Commons; the steady-cadence
  default.
- **IP.com Prior Art Database** — paid per document, but indexed in the
  tools patent examiners actually search, fast publication, timestamped
  disclosure number; use for the capabilities most likely to face a
  blocking patent.

## Leg 3 — xrefs, both ways

Every disclosure artifact is cross-referenced:

- **In-corpus**: the feature's design doc / findings / designs get a
  typed link (rel `published-as` / provenance ref) carrying venue +
  document id (DOI, TDCommons id, IP.com disclosure number). FTO
  coverage becomes queryable in both directions.
- **In the disclosure**: the report cites the precis artifacts it draws
  from plus related patents/papers the patent/paper kinds already index,
  so the prior-art chain is auditable from either end.

## The closed loop — gap check

A periodic view/query lists shipped capabilities with **no** disclosure
xref: the undisclosed backlog surfaces itself instead of being
remembered. Candidate home: the routine lane alongside the capability
landscape's auto-refresh.

## Open decisions

- Zenodo cadence + deposit licence.
- Which artifact classes qualify as capability-class (a short rubric).
- IP.com budget threshold (when is a capability worth the fee).
- Where the draft-for-review lands (draft kind? email to Reto? the
  approve queue).

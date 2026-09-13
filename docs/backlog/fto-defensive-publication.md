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

## Policy — no patent reading for commercially-used capabilities

Reto, 2026-09-13. For anything we intend to use commercially, **do not
search or read patents** — not people, not agents. Knowledge of a
specific patent is what turns ordinary infringement into willful
infringement (enhanced/treble damages, US); staying unexposed is the
standard defensive posture. How we deal with FTO instead:

- **This SOP is the FTO instrument**: dated defensive publication makes
  our own work prior art; we don't clear against others' patents.
- If a patent search ever becomes necessary (a real threat letter, a
  licensing decision), it is **counsel-directed and privileged** — never
  an agent task, never ad-hoc.
- **Collision with Leg 3**: comment 1 on gr334824 has disclosures citing
  "related patents … already indexed by the patent kind". For
  commercial-use capabilities that leg is papers/corpus-artifacts only;
  patent citations in a disclosure only where the capability is
  research-only, or via counsel. The patent kind itself stays (it serves
  research corpora); the *agents drafting disclosures* must not be
  pointed at it for commercial capabilities.

## Audit 2026-09-11 — current state, filing list, sidesteps

Checked live (session, worktree zesty-stargazing-lecun):

- **Zenodo: zero deposits** for precis-mcp.
- **Tags on origin run to v8.4.3; GitHub releases stop at v3.0.0**
  (2026-04). Zenodo's GitHub integration triggers on *releases*, not
  tags — release-cutting lapsed, so the webhook alone would deposit
  nothing.
- `CITATION.cff` exists (Zenodo reads it — no `.zenodo.json` needed) but
  is stale at 3.1.1. Licence GPL-3.0-or-later; deposit mirrors it.
- Software Heritage archival status unverified (API timeout).

### File now (near-zero drafting)

1. Reto flips the Zenodo GitHub-integration toggle for
   `retospect/precis-mcp` (OAuth, user-side).
2. Bump `CITATION.cff` version, cut a GitHub release on the latest tag →
   auto-deposit → **first DOI** (half the gripe's exit condition).
3. Software Heritage save-code-now request — free second timestamped
   archive.
4. Wire release-cutting to the existing tag path (`gh release create` in
   `scripts/bump`, or a release-on-tag Action). No sync job — the
   webhook does the rest.

### Draft for review, then file (TDCommons, free)

Priority by blocking risk:

1. Guided PCB place+route pipeline (slices 1–8, 10 shipped) — commercial
   EDA is heavily patented space.
2. se structural-envelope planner incl. null-space prestress DRC /
   m−s stability-classifier surface.
3. nm molecular-machine kind — envelope fit, graded module placement.
4. Nanopub claim pipeline — taproot graduation, evidence edges, OTS
   provenance.
5. catpath GPU reaction-pathway engine.
6. Blocktree state machinery — spec `ready`, unshipped; disclosure
   doesn't require working code — file from spec or wait for ship
   (Reto's call).

### Sidesteps

- **The Zenodo snapshot is itself a bulk disclosure**: the public tree
  carries code + package-docstring prose for every shipped capability,
  so one DOI-stamped deposit dates all of them at once. Reserve
  TDCommons reports for the highest-risk few (PCB, se) instead of one
  per ship. Caveat: backlog design docs are delete-on-ship — for older
  ships the rich prose is only in git history, which a release tarball
  doesn't capture; those are the ones worth a written report.
- **AM-filter SIMP needs no new report** — the topo-place-route preprint
  (td266176) covers it; it sits under Reto's standing hold, and
  releasing it is the cheapest high-value disclosure available (Reto's
  decision).
- **Published nanopubs** (fi211520 onward) already are dated public
  disclosures of the claim content; in-repo OTS machinery can stamp
  release tarballs for free extra timestamp proof (optional).
- **Skip IP.com for now** — hold the paid venue until a specific
  capability faces a specific threat (resolves the budget-threshold
  decision as "deferred").

## Open decisions

- ~~Zenodo cadence + deposit licence~~ → per-release on the existing tag
  flow; deposit licence mirrors GPL-3.0-or-later.
- Which artifact classes qualify as capability-class (a short rubric).
- ~~IP.com budget threshold~~ → deferred until a concrete threat; and
  per the no-patent-reading policy above, identifying such a threat is
  counsel-side, not ours.
- Where the draft-for-review lands (draft kind? email to Reto? the
  approve queue).

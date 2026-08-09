---
status: draft
title: Taproot hub-refine — prod enablement + follow-ons (contradicts edges, memo safety, grounding depth)
model: opus
---

# Taproot hub-refine — enablement + follow-ons

The pass is **BUILT, dark**: `src/precis/workers/hub_refine.py` (claim →
discover → filter → verify → write → stamp, converging via idempotent
attach + edge-exists precheck + `meta.taproot_rejected` rejection memo) and
`src/precis/workers/chase_trigger.py` (the ingest-triggered watermark:
reverse-ANN of freshly-embedded chunks against `claim_embeddings`,
migration 0101, marks near hubs `TAPROOT_DUE`; the claim query is a
due-set with a `PRECIS_TAPROOT_REFINE_BACKSTOP_H` failsafe). Design +
convergence rationale: git history of
`docs/backlog/taproot-hub-refine.md`; present state:
`docs/architecture/state-map.md` §hub-refine and the module docstrings.
The *why* is `taproot.md`; hub-refine also carries the citation-following
Discover source (shipped — see `hub_refine.py`).

Remaining work is enablement + the follow-ons below (see Residuals for
the concrete enablement steps and gotchas).

## Follow-ons

- **Attach true contradictors as `contradicts` edges** (ADR 0073) instead
  of dropping them — lights the living cite's contradictor list and feeds
  the Phase-4 "your claim broke" alert.
- **Conflict-safe `taproot_rejected` memo write** (defence-in-depth
  against lost-update when two passes touch one hub).
- **Grounding-depth policy** (Reto, fi189527): abstract-only grounding is
  fine for definition/existence claims; measurement/mechanism claims want
  a body-passage corroborator — fold into the refine design.
- v2 notes kept from the build ticket: `TAPROOT:saturated` long-backoff
  after K empty passes; paper-version memo invalidation; a queryable
  `taproot_evidence_judgment` table if judgment analytics are wanted.

## Residuals (from OPEN-ITEMS)

- Enable in prod (Phase 2): `hub_refine.py` + `chase_trigger.py` ship dark.
  Runbook: `docs/runbooks/taproot-chase-enablement.md`. The flip MUST be a
  single-host `service_config` prio override, never the shared role env —
  both passes have empty default_profiles and the agent profile deploys to
  two hosts, so the plist env would run two instances (double-claim →
  duplicate verify + lost-update on `meta.taproot_rejected`). Embedder-warm
  blocker cleared; CHASETRIG_VERSION 2 re-sweeps cold-index chunks.
- Before enabling: re-run the full slice_refine_eval on the deployed v2
  rubric — hub 176363 drops its contradicting partials, 176272/176360 keep.
- Attach true contradictors as `contradicts` edges (ADR 0073) instead of
  dropping them — lights the living cite's contradictor list and feeds the
  Phase-4 "your claim broke" alert.
- Make the `taproot_rejected` memo write conflict-safe (defence-in-depth).
- Grounding-depth policy (Reto, fi189527): abstract-only grounding is fine
  for definition/existence claims; measurement/mechanism claims want a
  body-passage corroborator — fold into the refine design.

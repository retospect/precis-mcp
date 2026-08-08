# taproot-hub-refine

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

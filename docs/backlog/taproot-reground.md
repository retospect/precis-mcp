---
status: idea
title: taproot reground residuals — slice_refine_eval gate, retire prose-edit modes, external legs
---

# Taproot reground — residuals

The reground extension shipped dark inside `hub_refine.py` (strict judge,
fisheye audit, prune planning, same-paper + external re-discovery,
`taproot/hub.py` removal door, `reground_claim` job glue). Gates:
`PRECIS_TAPROOT_REGROUND` (service loop), prune interlocked on
`PRECIS_TAPROOT_REGROUND_PRUNE` carrying the `slice_refine_eval` token on
BOTH the service and job paths (incl. `mode=verify, repair=true`), external
+ retire separately gated. Residuals, ranked:

1. **`slice_refine_eval` rubric gate — the live blocker on enabling
   prune.** Pass criterion from the pilot: hub 176363 must drop its
   contradicting partials; 176272/176360 must keep theirs. Until it
   passes, the interlock stays closed everywhere.
2. **Retire prose-edit modes are stubbed** (`_flag_retire`): the
   double-gate, draft-citer census, durable `meta.reground_verdict` with
   proposed reword, and worklist tag exist; the three per-paragraph edit
   modes (reword-in-place / replace-with-fact / stitch-delete) are not
   built. Nothing deletes, so no dangling-cite risk while stubbed.
3. **Perplexity external leg** returns `[]` (seam
   `RegroundConfig.external_probe_fn` is live; S2 + DOI mining are real).
   Fired on zero of the pilot's ten hubs — build only if external
   re-discovery under-delivers.
4. **DOI acquisition is record-only**: unheld mined DOIs land in
   `meta.reground_external`, never auto-`put` (unbounded, cost-bearing;
   the spec's paywalled Krishnan 1997 / fi189542 case is a human call).
5. **Conflict-safe memo writes**: `taproot_rejected` / `reground_log` /
   `reground_seen` are meta read-modify-writes, and `remove_evidence`'s
   strand guard reads without a row lock — all safe only under the
   documented one-host enablement rule (see `append_reground_log` and
   `remove_evidence` docstrings). Needed before multi-host enablement.

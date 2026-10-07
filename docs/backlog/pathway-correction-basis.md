---
status: draft
title: Compare pathway margins only under the same correction policy
pillar: 3d-design
---

# Compare pathway margins only under the same correction policy

Owner: catalysis-selectivity, item 24. Written 2026-10-04 as an adoption
blocker for catpath 0.24.0; Reto took 0.24.0 on 2026-10-07 (rev-pinned at
the v0.24.0 tag, corrections ON by default) with the gap still open, so
this is now a live comparison hole: two 0.24.0 margins with different
`gas`/`h_star` switches compare as equal today. Items 23/25 hold reruns,
backfill and the hydride pilot.

## Checked premise

`compute._network_basis` stamps digest/template/engine version;
`frontier.same_network_basis` ignores corrections. Catpath 0.24.0 records
`results.corrections` and permits independent `gas`/`h_star` switches.
Equal topology and engine version therefore do not imply equal energy
conventions. `tests/test_pathway_correction_basis.py` pins the gap with
strict expected failures; these are an adoption blocker, not validation
of a shipped comparison guard.

## Proposed comparison contract

Keep the existing topology/engine comparison and additionally compare a
versioned correction-policy tuple, with exact equality (no energy tolerance):

`(identity_schema=1, table_version, gas_enabled, h_star_enabled,
host_selection, model_keys)`.

- Booleans come from the recorded result, not today's defaults. Missing,
  malformed or config/result-disagreeing provenance on an engine >=0.24
  is unknown; unknown never compares, even with another unknown.
- `table_version` is the recorded corrections version when either switch
  is on. Catpath `corrections.VERSION` promises to change for every value,
  key or gauge change. Both switches off normalize this field to null.
- With H* enabled, `host_selection` is `auto` when the pathway's saved
  `config.corrections.host_metal` is null/absent, else `explicit:<element>`.
  With H* disabled, normalize it to null. Do not substitute the resolved
  `results.corrections.h_star.host_metal`: automatic Pd and automatic Cu
  are candidates evaluated under the same policy, although only Pd has
  an anchored shift.
- `model_keys` is the sorted unique `(backend, model)` set from enabled
  sections' `sets[*].key`, `gas.unknown` and `h_star.unanchored` (first
  two components only). Both switches off normalize it to empty. Ignore
  serialization order, duplicate keys and the set dictionary's labels.
- Exclude candidate-specific `state_shifts`, resolved host, anchored versus
  unanchored host status, and citation/source prose. The table version
  identifies the entire correction policy, not the resulting energies.
  Conflicting numeric records under one table version violate the engine
  contract; verify released fixtures against that table before adoption.
- Two pre-0.24 legacy stamps keep the current behavior. A >=0.24 missing
  record is not silently equivalent to explicitly disabled corrections.

## Test matrix

With equal digest/template/engine, comparison is symmetric:

| Difference | Equal? |
|---|---|
| gas on/off or H* on/off | no |
| correction table version, either switch on | no |
| auto host versus explicit Pd, H* on | no |
| contributing model keys, corrections on | no |
| known versus missing record, or two missing >=0.24 records | no |
| state shifts, resolved automatic host, source prose | yes |
| dictionary/list order or duplicate model keys | yes |
| table/model/host fields with both switches off | yes |
| explicit host difference with H* off | yes |

## Integration boundary

Add the policy to per-measure stamps and apply it before margins enter
the confirmed frontier. Do not choose a winning policy by candidate
iteration order: `current_network_basis` currently breaks same-engine
ties that way. The integration must use the quest's explicit requested
policy or report the conflicting cohort as provisional until selected.
Policy selection is still open; no production metadata edits here.

Remove strict xfails when the implementation satisfies the matrix; add
harvest/frontier tests for unknown provenance, per-measure demotion and
order-independent conflicting-cohort handling. No legacy backfill or
experiment runs are authorized by this spec. Review the proposed tuple
before implementation; it is not yet a persisted response contract.

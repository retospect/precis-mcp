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

## Open questions / decisions log

Ready-check 2026-10-21 (ready agent; checked against `compute._network_basis`, `frontier.same_network_basis`/`current_network_basis`/`apply_network_demotion`, `tests/test_pathway_correction_basis.py`, catpath `corrections.py`/`pipeline.py` record shape, and `kinetics-engine-v2.md`). Verdict: needs-work, 3 blockers, 6 advisories, no split.

- blocker: Policy selection is an unresolved open loop ("Policy selection is still open"; "Review the proposed tuple before implementation; not yet a persisted response contract"). The integration cannot be built without choosing the reference policy for `apply_network_demotion` (quest's requested policy from where? what if the quest states none?) and what "report the conflicting cohort as provisional" means in flags/measures.
- blocker: Integration acceptance is unverifiable. The matrix covers only the pure comparator; "harvest/frontier tests for unknown provenance, per-measure demotion and order-independent conflicting-cohort handling" name no expected outputs. Not stated: stamp key name and shape inside `network_basis` (e.g. a `correction_policy` key, hashed tuple vs dict), and whether it is also part of `network_basis_by_key` for every `NETWORK_DEPENDENT_KEYS` measure.
- blocker: Un-restamped stamps are undefined. Candidates already harvested under 0.24+ carry a `network_basis` with no policy key. `environment_basis` precedent (`same_network_basis` docstring, kin-s1 ruling): absent key is compatible with anything. This spec's rule "a >=0.24 missing record is unknown, never compares" conflicts if applied to an absent stamp key (would demote the whole frontier on deploy). Say which: absent key = legacy-compatible (as environment_basis) or demoted, and whether a re-harvest is required.
- advisory: Stale premise text. Spec says Reto took catpath 0.24.0; `pyproject.toml` now pins v0.32.0 (+aeb3fa3). Re-confirm the `results.corrections` shape (verified present in `pipeline.py`: `enabled`, `sets`, `unanchored`, `host_metal`, `state_shifts`) and update the version wording.
- advisory: "config/result-disagreeing provenance" is undefined. Name the compared fields (`config.corrections.gas|h_star` vs `results.corrections.<part>.enabled`) and what an absent config key means (catpath default is ON, which clashes with "not today's defaults").
- advisory: "verify released fixtures against that table before adoption" has no target, no pass criterion; make it a named test or drop it.
- advisory: Version gate ">=0.24" needs a rule for `autocatpath_version` null/unparseable (`_version_key` returns `()`); legacy or unknown?
- advisory: Sibling coordination. `kinetics-engine-v2.md` (D1 "stays unbuilt and is not a dependency", D3 derived `table_version`, D10, D11 "gains `tpd_set_sha256`") extends this tuple; `electrochem-mlip-ladder.md` and `electrochem-kinetics-beyond-che.md` also say they join it. Add a line here that this spec lands first and that `table_version` may become a derived stamp (v2 D3). Note v2 is internally inconsistent (D3 folds the TPD sha into `table_version`; D11 adds a separate `tpd_set_sha256` field); not this spec's bug.
- advisory: No `model:` set (defaults sonnet) and no In scope / Not in scope / Target + blast radius sections. Content is contract design plus frontier-integration judgement (opus-sized); list targets: `compute._network_basis`, `frontier.same_network_basis`, `current_network_basis`, `apply_network_demotion`, harvest stamping, the xfail test file.
- Subsumption by kinetics-engine-v2: NOT subsumed. v2 explicitly keeps this spec as a separate, unbuilt item and extends the tuple (v2 D1 Basis field, D3 `VERSION` derivation, D10 "extends, does not replace", D11 last bullets, Target + blast radius). v2 builds the environment basis, not the gas/H* correction-policy comparator, the matrix or the strict-xfail removal.

### Decisions (Reto 2026-10-21)

Not subsumed by the kinetics v2 basis fold: `kinetics-engine-v2.md` D1
says this item "stays unbuilt and is not a dependency", D10 "extends, does
not replace", D11 adds `tpd_set_sha256` to this tuple. Order: this item
first, v2 extends it. Blocker 2 (integration acceptance) a builder writes
from code (`network_basis.correction_policy` set through
`network_basis_by_key`; xfails removed; three named harvest/frontier tests).

1. Reference policy: the quest's explicit config (`corrections.gas`,
   `corrections.h_star`, host override if set). With none stated there is
   no reference, and every candidate whose tuple differs is provisional
   (measure moved to `flags['<key>_untrusted_value']` as
   `apply_network_demotion` does). Never candidate iteration order.
2. Working default listed with the question (not individually ruled):
   absent policy key = legacy-compatible (the `environment_basis` precedent
   in `same_network_basis`); only a present null/unknown value never
   compares; no re-harvest.

Buildable now.

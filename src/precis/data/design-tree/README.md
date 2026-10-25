# design-tree manifests — the design knowledge graph

Authored YAML for the design knowledge graph (gr516958, gr516957). Each
file holds goal roots whose nested `children:` trees are validated by
`precis.design_tree`, imported as `finding` refs tagged
`SPACE:design-conventions` by `precis conventions import --manifest
design-tree`, and rendered to the packaged `design_rules.json` that
`precis_se.joints` (`params.leaf`) and the DRC slices read. The export is
generated from these manifests — never from the live graph:

    uv run python -m precis.design_tree src/precis/data/design-tree \
        > src/precis/data/design_rules.json

## Files

- `join.yaml` — join (deep) plus locate (deep): the pilot tree
- `rotate.yaml`, `close.yaml` — deep function trees (run 2)
- `principles.yaml` — universal per-part checks, hand-curated `checks`
- `processes.yaml` — process tree under a `process` goal, DIN 8580 groups
  as methods (run 3 / slice S6)
- `axes.yaml` — the shared sub-choices (locking, captive, corrosion,
  bearing-face, material-direction) every leaf considers
- `failure-modes.yaml` — the `issue.failure_mode` registry
- `criteria.yaml` — the `asks` / `when` / `avoid_when` question vocabulary
- `functions.yaml` — the remaining function goal roots at two levels,
  `coverage: shallow` throughout, hand-authored

## Provenance and licence

Content was pre-filled with Perplexity (`perplexity-reasoning`) to find
sources, then every rule was verified claim-by-claim against its cited
source by a separate agent before merge (~87% of claims confirmed across
the three runs; wrong or unsourced numbers were corrected or dropped —
the per-node red lists wait on the human review). Facts and figures trace
to each rule's `source_url`/`standard`; all prose and rule texts are our
own wording, so these manifests are facts plus citations under the
project's licence, not copied text.

Standard tables are referenced, never restated: the shared ISO 2768-1
general-tolerance table is `src/precis/data/iso_2768_1.json`, fit classes
are `fit_classes.json`, and a rule cites them by its `standard` field.
Equipment-specific numbers (the `machine_specific` rules) live in the
`design-tree/capabilities.json` rows each rule's `table_ref` names; the
user's own test prints are the clearance authority.

AI-use disclosure (SPEC ruling 7): the content manifests — `join.yaml`,
`rotate.yaml`, `close.yaml`, `principles.yaml` and `processes.yaml` —
were pre-filled by an AI model (Perplexity) to find sources and verified
claim-by-claim against the cited sources by AI agents; the human gate is
the red-list review. The vocabulary manifests — `axes.yaml`,
`criteria.yaml`, `failure-modes.yaml` and `functions.yaml` — are
hand-authored (each header says so) and carry no rules or sources.
`capabilities.json` and `iso_2768_1.json` are data generated from the
manifests / transcribed from the standard, not prose.

# precis-mcp / axes

Machine-readable axis definitions for the paper auto-tagging system.
Each YAML file defines one axis: vocabulary, applicability gate,
LLM prompt, and schema version.

The classifier runner (`scripts/classify/classify-papers`, future
`precis jobs classify-papers`) loads every `*.yaml` here, walks the
paper corpus, applies the gate, calls the LLM (or skips), and writes
results to `refs.meta.processing.<axis>` plus an open tag
`<axis>:<value>` on the ref.

See `data/skills/precis-paper-tag-axes.md` for the human-facing
taxonomy doc.

## File schema

```yaml
id: scale                       # axis name; tag prefix
version: 1                      # bump to trigger re-classification
question: "What spatial scale...?"
values: [atomic, nano, meso, micro, bulk, multi, unknown, n-a]
default_unknown: unknown        # value used when LLM is unsure
applies_when:                   # ALL conditions must hold
  domain_in: [chemistry, physics, materials, eng]
prereq: [domain]                # axes that must run first
cost_tier: small                # small | medium
prompt: |
  ...one-shot system prompt for the LLM...
```

`applies_when` predicates supported:

- `domain_in: [list]` — checks `meta.processing.domain.value`
- `journal_matches: [glob, ...]` — checks `meta.journal`
- `abstract_mentions_any: [str, ...]` — case-insensitive substring
- `always: true` — runs on every paper (default if `applies_when` omitted)

Predicates AND together. Add new predicates by editing the
`Applicability` checker in `scripts/classify/_runner.py`.

## Versioning

`version:` is the only knob that triggers re-classification. The
runner reads `meta.processing.<axis>.v` and skips refs ≥ current.
Bump version when:

- vocabulary changes (new value, removed value, renamed value)
- prompt changes meaningfully (not for typos)
- applicability gate widens to include previously-skipped papers

Don't bump for prompt typos or comment-only edits.

## Adding a new axis

1. Drop `axes/<name>.yaml` here. Use an existing axis as template.
2. Add the axis to `precis-paper-tag-axes.md` (taxonomy doc).
3. Add hand-labels for the new axis to `gold_set.yaml`.
4. Run `eval-classifier --axis <name>` to confirm ≥ 85%.
5. Run `classify-papers --axis <name>` for the bulk pass.

No code changes required. No migrations. Tags land in a per-axis
uppercase namespace derived from the axis id (`SCALE:10nm`,
`ROLE:result`) with the closed-vocabulary validation table loaded
from these YAML files at boot — NOT in the free-form OPEN namespace
(ADR 0047: OPEN is the human/agent folksonomy and is slated for
culling; curated tags must not stand in the blast radius).

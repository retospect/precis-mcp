# precis-mcp documentation

The front door to the `docs/` tree. Start with the must-reads, then
dive into the subdirectory that matches your task.

## Start here

| If you want to… | Read |
|-----------------|------|
| Contribute / change code (conventions, workflow, DoD) | [`../AGENTS.md`](../AGENTS.md) — **canonical** |
| A narrative system overview | [`architecture.md`](./architecture.md) — the manual |
| The present-tense map of live subsystems | [`../CLAUDE.md`](../CLAUDE.md) |
| The user-facing intro + install | [`../README.md`](../README.md) |
| The DB schema (generated, current) | [`design/schema.md`](./design/schema.md) |
| Why a decision was made | [`decisions/README.md`](./decisions/README.md) — the ADR index |
| What changed, when | [`../CHANGELOG.md`](../CHANGELOG.md) |
| The active backlog | [`../OPEN-ITEMS.md`](../OPEN-ITEMS.md) |

## The subdirectories

- **[`decisions/`](./decisions/)** — Architecture Decision Records,
  numbered, one per substantive trade-off. **Never deleted**; obsolete
  ones are marked superseded and kept for history. The
  [index](./decisions/README.md) carries the by-topic table +
  supersession graph.
- **[`design/`](./design/)** — plan artifacts, one per non-trivial
  change (schema, new CLI subcommand, new handler, …). Obsolete plans
  stay for context — treat a dated plan as point-in-time intent, not
  current state, unless it says otherwise. Notable: the generated
  [`schema.md`](./design/schema.md), the prose
  [`storage-v2.md`](./design/storage-v2.md), and the visual
  [`schema-v2.puml`](./design/schema-v2.puml) (conceptual sketch).
- **[`user-facing/`](./user-facing/)** — external specs for agents and
  API consumers: the verb-surface migration, the edit protocol,
  unified addressing, per-kind specs (patent / python / voice), plugin
  authoring, the paper-ingest path.
- **[`conventions/`](./conventions/)** — the rules that bite:
  [`thresholds.md`](./conventions/thresholds.md),
  [`kind-enablement.md`](./conventions/kind-enablement.md),
  [`discovery-layer-policy.md`](./conventions/discovery-layer-policy.md).

## Agent-facing docs

The LLM-facing manual is **not** here — it ships as help *skills*
under [`../src/precis/data/skills/`](../src/precis/data/skills/),
served at runtime via `get(kind='skill', id='…')`. Start at
`precis-overview` (kinds table + skill index) or
`precis-toolpath-help` (canonical call sequences).

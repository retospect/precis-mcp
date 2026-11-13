---
status: draft
title: finish copying every agent's local skills into precis skills
pillar: platform
prio: normal
---

# finish copying every agent's local skills into precis skills

Campaign for gr657041. Slice 1 (gr657064, td658267) inventoried every
agent's local skill set and copied them into `src/precis/data/skills/`,
merging duplicates in favour of what the code does today and marking
steering skills `governs: factory` (interim definition; slice 3 settles
it). This item tracks what is left and holds the slice-1 inventory the
gripe asked for as its done-check.

## Slice-1 inventory — every local skill and where it went

`.claude/skills/` (Claude; also loaded by Kimi via `extra_skill_dirs`):

| local skill | precis skill | disposition |
|---|---|---|
| backup-restore-drill | `backup-restore-drill` | copied; `pgvector`/`chunk_embeddings` names elided to placeholders — the corpus's internal-name gate bans them; the runbook `docs/runbooks/backup-restore-drill.md` keeps the literals |
| boss-design | `boss-design` | copied; tables verbatim |
| bug | `bug` | copied |
| coder-chain | `coder-chain` | copied |
| differentiation | — dropped, merged | was a shim deferring to `precis-differentiation-help`; alias `differentiation` → `precis-differentiation-help` added, its one unique fact (the `src/precis/structsolve/simp.py` gating anchor) folded into the product skill |
| docs-triage | `docs-triage` | copied |
| flow | `flow-stages` | copied, renamed: a single-word file slug cannot pin its own query (it ranked below `precis-se-flow-help` and dead-ended the neighbour test) — the hyphenated slug pins and stays discoverable |
| gripe-fleet | `gripe-fleet` | copied; H2 structure added for the 4000-char chunk cap; `scope: write` matching `precis-gripe-loop-help` |
| quiet-output | `quiet-output` | copied |
| reconsolidate | `reconsolidate` | copied |
| remember | `remember` | copied |
| se-design | `se-design` | copied; the `rd` coverage compressed to its rules + a pointer into `precis-se-help` (which owns the op grammar); sharp-edge caveats kept with a check-the-live-gripe hedge |

`.claude/commands/` (Claude slash commands; Kimi `/skill:<name>`):

| local command | precis skill | disposition |
|---|---|---|
| fleet | `fleet` | copied; `scope: write` |
| go | `go` | copied + `.devin/workflows/go.md` deltas folded into a Harness notes section; `scope: write` |
| land | `land` | copied + `.devin/workflows/land.md` deltas folded the same way; `scope: write` |
| next | `next` | copied |
| pillar-review | `pillar-review` | copied; `scope: write` |
| qgo | `qgo` | copied; `scope: write` |
| qland | `qland` | copied; `scope: write` |
| rebase | `rebase` | copied |
| round | `round` | copied; `scope: write` |
| surface-review | `surface-review` | copied |
| whatneedsdoing | `whatneedsdoing` + `whatneedsdoing-scans` | split: the 34.7 KB source exceeds the ~22.5 KB serve cap — the step-3a scans catalogue became `whatneedsdoing-scans`; `.devin/workflows/whatneedsdoing.md` deltas folded in |
| workspace-cleanup | `workspace-cleanup` | copied |

`.devin/workflows/` (Devin): `go`, `land`, `whatneedsdoing` — folded into
the ported command skills above as Harness-notes content; no separate
skills.

## Considered and dropped — not skills

| path | why not ported |
|---|---|
| `.claude/agents/*.md` (19 remits) | harness agent definitions the ported commands name by remit; harness-bound, not skills — each runtime maps them per `.kimi-code/AGENTS.md` |
| `AGENTS.md`, `CLAUDE.md`, `.kimi-code/AGENTS.md` | resident briefs / per-harness shims, not skills |
| `.claude/settings.json`, `.devin/hooks.v1.json`, `.mcp.json`, `.codex/config.toml`, `scripts/hooks/` | harness configuration and hooks, not skills |
| `.claude/fleet/` | coordinator working files (message templates, roster state), not skills |

## What remains

- **td657071 (human step, staged not applied):** the artifact is
  attached on td657071, not committed — the gripe stages it for Reto to
  review and run (`Do not apply or commit it`). `trim-agent-local-skills.py`
  (idempotent, dry-run default) rewrites each migrated local
  skill/command file to a bootstrap stub pointing at the precis slug,
  and `skill-guard-governs-factory.patch` switches `skill.py`'s
  repo-dev guard from the `.claude/skills` dir list to the served
  `governs: factory` tag set.
- **Slice 2+:** whatever the parent gripe defines next (the remaining
  agents' equivalents if any surface, plus settling `governs:` as a
  first-class axis — slice 3's job per the mint).
- **`_REPO_DEV_SKILLS`:** still the pinned `.claude/skills` dir list and
  still test-correct (dirs exist until td657071 runs); the trim script's
  guard patch supersedes it.

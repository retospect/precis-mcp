---
status: ready
title: skills prose cleanup — ratchet gate, ops-doc gutting, four splits, review-family unification
prio: normal
model: sonnet
---
# skills prose cleanup — ratchet gate, ops-doc gutting, four splits, review-family unification

Residue of the 2026-09-16 eight-agent review of every skill (dev `.claude/` +
product `src/precis/data/skills/`). Batches A+B (12 verified-wrong examples,
dev prompt fixes) shipped as `fix(skills): correct 8 wrong verb examples …`
(main cc9b3ef5). This item is batches C–F. Convention under test:
`docs/conventions/skill-authoring-style.md`.

## Motivation / why

Cluster agents copy skill examples verbatim; a wrong kwarg costs a round trip
per call. Skill bodies are served raw from the wheel (frontmatter stripped,
`{{include}}` and `[[wikilink]]` expanded, nothing else), so every ADR number,
env var and CLI line reaches the agent as text it cannot act on.

## In scope

**C. Ratchet gate — `tests/test_skill_prose.py`** — LANDED 2026-09-17.
Five checks (ADR ref, backlog path in prose, operator affordance, unfenced
verb call, alias overrun >4) with a frozen per-file allowlist that only
shrinks; D–F drain it. Gripe ids are valid handles — not gated; bare-noun H2s
are an advisory warning. Frozen counts: adr 26 files / backlog 14 / operator
25 / unfenced_verb 2 (audio, figure) / alias_overrun 2 (anki, status).

**D. Gut ops-doc skills to verb-facing lines**; move removed prose verbatim
to `docs/runbooks/<kind>-ops.md`, link from the owning package docstring.
Keep every sentence naming a verb the agent can call or the next call on a
miss. Targets (est. cut): settings-help 90%, health-digest-help 75%,
news-help 55%, datasheet-help 45%, minter-help 45%, audio-help 40% (also
drop hostnames), alert-help "Lifecycle (producer side)", fix-gripe-help
"Trust model". Same batch: delete negative-laundry sections (research-help
"Anti-patterns", anki-help "What this is NOT", cad-help "Scope (v1)",
se-help "Known sharp edges"), the three "by design" lines
(paper-tag-axes, structure-help, gripe-help), nm-kind history in se-help,
LaTeX-recovery postmortem in draft-help "Data / table chunks", 2026-08-19
narration in notation-canon, measured-corpus stats in taproot-mint-help
"Claim admissibility" and finding-help "How settled is this claim?",
pathway-help web-explorer section → one line, toon "Parse … in Python" +
"Pick a format on the CLI" sections.

**E. Splits + recipes.** One coder per file (coder-chain), `scaffold` mints
siblings, wikilinks corpus-grepped after each move:
- draft-help (39K) → `precis-draft-rich-content-help` (Figures, Data/table)
  + `precis-draft-export-help`; prose-craft half of "Writing well" folds
  into write-paper-help; "Cite a paper we don't have yet" 62→~15 lines.
- se-help (29K) → `precis-se-atomic-help` (all atomic-mode content).
- cad-help (31K) → `precis-cad-assembly-help` (ports/mate/payload/joints)
  + `precis-cad-build-help` (make-tree/dim/material-mass/BOM).
- taproot-mint-help (24K) → `precis-taproot-hub-edit-help`
  (sharpen/refine/merge, attach evidence, reword).
- todo-tree-help: add H2 "Claim a doable leaf before working it" — the
  `claimed-by:<handle>` CAS lease (`handlers/todo.py`) is documented as a
  state, never as the `tag(...)` call; verify the call shape first.
- Own each duplicated fact once: `more(cursor=)` rule (4 files → toon),
  id-vs-q rule (overview owns; toolpath drops "Rule of thumb"), refs.bib
  find-replace (edit-help owns), component-help unit/band rules → one line +
  [[precis-material-help]], startup-skills vs session-context pinned-skills.
- Small verified fixes not in A: tag-help "remove= … no-op" sentence
  (universal restatement); folder-help bare fences → python; edit/delete/
  plaintext fence tags; delete-help "Undo a delete" needs a next call;
  structure-help "continued" H2 → goal-voice; calc-help H2 drops `pint`.

**F. Review family** (after E). First list the 17 `{{include}}` sites —
overlap that is one source spliced twice is not a finding. Then: standardise
the four review-* skills on common-reviewer's blocker/high/medium/low;
review-paper-help's quality bar → pointer to the adversarial persona; wire
review-paragraph-flow + review-section-structure into polish-paper's
`invokes-personas` (or drop its "more personas land here" line).

## Explicitly NOT in scope

Bare-noun H2 rewrites corpus-wide (advisory only); hostname gate scope
outside `deploy/`; `whatneedsdoing.md` 25-line transcript-persistence
paragraph → runbook (nice-to-have).

## Acceptance criteria

- `tests/test_skill_prose.py` green with a frozen allowlist; allowlist
  shrinks with each of D/E/F.
- Every gutted skill's removed prose is findable in `docs/runbooks/`.
- All existing skill tests + ingest gate green; no dangling wikilinks.
- One `/go` deploys; `get(kind='skill', id='precis-get-help')` on prod shows
  `page=2`.

## Target + blast radius

`src/precis/data/skills/**`, `tests/test_skill_prose.py`, `docs/runbooks/`.
No code paths. Sibling worktrees editing skills (gripe-fix sweeps) will
conflict on the big four — check `scripts/inflight` before each split.

## Open questions / decisions log

- 2026-09-16 Reto: split names as listed above; severity scale =
  common-reviewer's; leave the secret gate's hostname scope alone.

---
status: idea
pillar: memory-graph
---

# Skills redesign — remaining: quality gates, schema includes, authoring triplet

Substrate shipped: `src/precis/handlers/skill.py` and
`src/precis/ingest/skill_ingest.py` docstrings; design in `git log`.
Prose conventions: `docs/conventions/skill-authoring-style.md`.

## Remaining scope

1. **Static gates (hard-fail at ingest)** — the subset not yet enforced by
   `skill_ingest`: `FLAVOR:persona` bodies contain an `## Adopt this
   persona` H2; `FLAVOR:runbook` `invokes_personas:` resolve; H2
   bare-verb-nominalisation heuristic; schema-drift backstop (example code
   blocks in `FLAVOR:reference` skills reference arguments that exist in
   the current `tools/core.py` schemas); per-chunk embedder budget check.
   A failing skill does not ingest; the boot scan logs file+line and the
   previous version stays live.
2. **`{{include schema:…}}`** — render verb/param schemas from
   `tools/core.py` into skill bodies so reference skills can't drift from
   the real signatures. Follow-up: extend to handler-side kindspec args
   (e.g. `citation`'s named kwargs).
3. **LLM gates (soft-fail as gripes)** — scheduled/CI judgment checks
   emitting `kind='gripe'` linked to the offending skill: H2 voice ("reads
   naturally after 'I want to…'"), alias-group spread, persona
   authenticity, example-prose agreement, H2 self-sufficiency.
4. **The authoring/review triplet** (shipped skills):
   `precis-skill-author-best-practices` (reference — the rules),
   `precis-doc-writer` (persona, authoring time), `precis-skill-reviewer`
   (persona, runs the LLM gates → gripes). Run via a periodic worker pass
   or a `precis lint skills` one-shot.

## Deferred open issues

- Multi-description authoring conventions (sibling alt-descriptions per
  chunk) — defer until gripe-feedback data motivates the shape.
- Reviewer-finding wiring into `kind='finding'` — revisit when the
  polish-paper runbook is authored.
- Stable deep-links across heading edits — two-tier addressing
  (`~N` + `#h2-slug`) is the v1 answer; revisit only if it breaks.

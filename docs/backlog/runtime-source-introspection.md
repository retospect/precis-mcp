---
status: draft
title: Should precis have read access to its own source at runtime?
model: opus
---

# Should precis have read access to its own source at runtime?

> Migrated from gripe 51209 (area:architecture). This is an **open
> architecture question**, not a decided change — the proposal is the
> question and its arguments, not a build plan. Do not treat "in scope"
> below as authorization to implement; it frames what a future decision
> would need to settle.

## Motivation / why

Precis-mcp currently cannot introspect its own implementation while running.
A cluster agent asking "how does this kind work", requesting the schema for
a `kind=`, or hitting an error it can't self-diagnose has no path back to
the source that would answer it. The appeal: richer self-documentation,
error messages that cite the actual failing code path ("this failed because
the executor does X") instead of a generic message, and skill docs that
could in principle be generated from source instead of hand-maintained
(`src/precis/data/skills/`).

## In scope

The question being tabled, not a build:

1. **Whether** runtime source-read access is worth building at all, given
   `get(kind='skill')` already serves the self-documentation role at a
   higher (curated, product-facing) altitude.
2. **If yes, at what scope** — see the scope trichotomy below. This is the
   central open question a "yes" decision must resolve before any
   implementation is scoped.

## Explicitly NOT in scope

- **No implementation.** This is a decision-tabling spec. It does not
  design a handler, an API surface, or a skill; it does not pre-commit to
  building anything.
- **No default answer.** Neither "precis should read its own source" nor
  "skills are sufficient" is asserted here — both are live positions in the
  arguments below.
- **No scope commitment.** Full source vs. schema-only vs. docstrings-only
  is left open; picking one is the decision this doc awaits.

## Acceptance criteria

This proposal is "done" when a decision is recorded, not when code ships:

1. A build / don't-build call, made and stated.
2. If **build**: which scope — full source, schema definitions only, or
   docstrings only (see below) — and why the other two were rejected.
3. If **don't-build**: what closes the underlying appeal instead (e.g.
   "skills already cover this; strengthen skill-generation-from-source as a
   build-time step instead of a runtime read").

## Target + blast radius

Not yet applicable — no code is proposed. If a "build" decision lands, the
likely touch points to scope next:

- The **handler/skill layer** (`src/precis/data/skills/`,
  `get(kind='skill')` dispatch) — where a self-documentation surface would
  most naturally live, and the layer this question asks whether it
  duplicates.
- Whatever `precis-mcp` runtime process reads its own package source (a new
  seam — none exists today).
- The **deployed-vs-runtime source question** (see below) touches the
  release/deploy path (`docs/conventions/container-ops.md`,
  `scripts/ship`/`scripts/deploy`) if a decision requires the running
  process to assert its source matches what's deployed.

## Open questions / decisions log

**For:**
- Richer self-documentation surfaced live rather than hand-maintained.
- Better error messages — cite the actual failing code path ("this failed
  because the executor does X") instead of a generic message.
- Skill docs could be generated from source rather than hand-maintained,
  closing a drift class this repo otherwise polices by convention
  (`CLAUDE.md`'s "skills are runtime docs" rule).

**Against:**
- Circular dependency risk — the introspection layer becomes something the
  system depends on to explain itself, complicating what "self" means at
  runtime.
- Security surface — source exposes internals (implementation details,
  possibly secrets-adjacent code paths) to whatever can call the verb.
- Runtime source may differ from the deployed binary — a running process
  reading "its own source" from disk isn't guaranteed to match what's
  actually executing (build artifacts, container layering,
  deploy-in-progress race).
- **Skills already serve this role at a higher level** — `get(kind='skill')`
  is the existing, curated, product-facing self-documentation channel. Open
  question: does a runtime-source-read feature duplicate that, or does it
  serve a genuinely different need (e.g. schema-exact answers vs. skill's
  hand-curated prose)?

**Scope trichotomy (if the answer is "build"):**
1. **Full source** — richest, largest security/circularity surface.
2. **Schema definitions only** — e.g. `PARAMS_SCHEMA` / kind field
   definitions; narrower, answers "what does this kind's shape look like"
   without exposing implementation logic.
3. **Docstrings only** — narrowest; answers "what is this for" without
   exposing schema or logic.

No decision recorded yet on build/don't-build, scope, or the
skills-duplication question.

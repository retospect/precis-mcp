---
status: draft
title: <one-line intent>
model: <optional — sonnet | opus | haiku; unset ⇒ fixer default (claude-sonnet-5)>
blocked-by: <optional — slug of a backlog item that must ship first>
snooze-until: <optional — YYYY-MM-DD; skipped by triage until then>
---

# <one-line intent>

<!-- A plain IDEA needs none of this: no front-matter, a title + a few
lines (what, why, owner anchor, test:). This template is for the SPECCED
form — status: draft while argued, status: ready when the fixer may pick
it (branch fix/<slug>). On ship: fold surviving truth into the owning
package docstring, DELETE this file in the same commit (git is history).
Split into separate items only when deliverables are independently
shippable; declare real ordering with blocked-by. -->

## Motivation / why
<the problem; the "why it's this way" paragraph of the owning package
docstring inherits this if the change ships.>

## In scope
<what this change does.>

## Explicitly NOT in scope
<the boundary — what a reader might assume but this does not do. The
`ready` gate flags overreach and deferred-as-in-scope here.>

## Acceptance criteria
<"done means X" — concrete, verifiable. Load-bearing: these become the
post-deploy blast-radius check, and `ready` will NOT pass without them.>

## Target + blast radius
<which handlers / verbs / routes / workers this touches — seeds the
post-deploy look and the doc-freshness check.>

## Open questions / decisions log
<`/ready` writes open questions here; you resolve them into decisions.
No blocker-severity open question may remain when `status: ready`.>

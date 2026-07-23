---
status: draft
title: <one-line intent>
model: <optional — sonnet | opus | haiku; unset ⇒ fixer default (claude-sonnet-5)>
blocked-by: <optional — slug of a proposal that must ship first>
---

# <one-line intent>

## Motivation / why
<the problem; becomes the ADR context if this graduates to a decision.>

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

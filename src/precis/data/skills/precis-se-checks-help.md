---
id: precis-se-checks-help
family: se
title: precis — se check reports: the validate digest, the check budget and follow-up checks
summary: what the se checks report back — the `validate:` digest on every put/edit, the 60 s `view='validate'` wall-clock budget (`check_not_run`), and follow-up checks a report suggests
answers:
  - what does the `validate:` digest on a put/edit reply mean?
  - why does a check read "not run: budget exceeded" / check_not_run?
  - which follow-up check should I run on my se design?
applies-to: get (kind='se', view='validate'|'drc'|'sweep')
status: active
tags: design
kinds: se
---

# precis-se-checks-help — check reports, budgets and follow-ups

Authoring lives in [[precis-se-help]]; the per-view check grammar is
[[precis-se-check-help]]. This file is what the checks *report back*:
the write-time `validate:` digest, the wall-clock budget
`view='validate'` runs under, and the follow-up checks a report
suggests.

## The `validate:` digest on every write

**Every `put`/`edit` reply ends with a `validate:` digest** —
`validate: clean`, or `validate: N error(s), M warning(s) — first:
<rule> <detail>; see view='validate'` — and `/se/<slug>` shows it above
the findings table — a write never refuses, but it no longer stays
silent. `view='drc'` prints the same digest as its last
line, and a DRC-clean design with validate warnings reads `no DRC
findings; validate has N warning(s)` — a clean DRC never implies a clean
design. For bound structures, `atom_clash` fires per block pair when posed atoms come
within 2 Å (error under 1 Å, warn in [1, 2) Å — count, min distance,
closest pair in the text). A `connect` does not excuse it —
re-pose the block (`set_pose`); blocks left at `[0,0,0]` pile up and
trip it.

## The `view='validate'` wall-clock budget

`view='validate'` runs under a 60 s wall-clock budget and returns within
about that: on field-backend designs (rd/blend/field leaves) the fdm
print-check meshes at a capped pitch and its orientation search stops at
the budget with the best-so-far (a `fdm print-check:` header note says
what was coarsened or cut), and any stage the budget doesn't reach reads
`not run: budget exceeded` / `check_not_run` — unchecked, never
silently clear.

## `view='suggest_checks'` — seeded verify ops

`view='suggest_checks'` seeds the verify loop from the design's
declarations — read-only `add_ray`/`set_measure` ops (or transient
`view='ray'` args): `bolt_axis` per threaded port, `region` per shaped
pocket, a cutter-axis probe per cad-bound cut, `set_measure` to a
checked same-unit measurand per `measurand_unchecked`. Top 6.

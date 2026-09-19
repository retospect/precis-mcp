---
status: draft
title: "source docstrings cite deleted docs/backlog/ files — ~60 dangling, no lint catches it"
---

# Delete-on-ship leaves citations behind

`docs/README.md`'s delete-on-ship rule is right — a shipped backlog item is
noise, and `git log` keeps it. But source docstrings cite those files by path
as design rationale, and nothing checks the citation when the file goes. The
2026-08-20 consolidation ship deleted ten items and left roughly **sixty**
`docs/backlog/<file>.md` references dangling across
`src/precis/taproot/{canon,hub,reground,backfill,trust,apply_migrate,migrate,eval_canon}.py`,
`src/precis/workers/{chase,chase_trigger}.py`, `src/precis/cli/{taproot,taproot_migrate}.py`,
`src/precis/store/types.py`, and ~10 test modules. That ship fixed only the
three it had directly edited.

None of this is a runtime defect — it is an agent reading a docstring for the
"why", following the pointer, and getting nothing.

**Recurring, not one-time.** The 2026-09-19 blocktree/se fold hit the same
pattern independently: `nm-se-merge.md` and `structure-unit-enclave.md` were
already dangling (cited across `src/precis/blocktree/__init__.py` and
`src/precis_se/__init__.py`, left out of that fold's scope) when
`blocktree-library-build-plan.md` and `port-pose-and-composition-search.md`
were deleted the same day, each cited by filename in 20+ places across
`src/precis_se/`, `src/precis/blocktree/`, and a dozen `docs/backlog/`
siblings. The docstring citations were repointed (option 2 below) as part of
that fold; the ~40 remaining source-comment citations to the two newly-deleted
files were left, matching this item's "burn down in passing" sequencing —
confirming the debt regrows between sweeps exactly as predicted.

## The convention question, undecided

Three options, and the repo currently does all three by accident:

1. **Repoint to the merge target** where the item was consolidated rather than
   completed (`taproot-atomic-claims.md` → `taproot-compound-migration.md`).
   Correct when the work is still open.
2. **Repoint to the present-state home** — the owning package docstring. Correct
   when the work shipped and the rationale now lives in the code's own docs.
   This is what the three fixed citations did (→ `precis.taproot` stage 5).
3. **Annotate `deleted — git log keeps it`**, the pattern `taproot-reground.md`
   uses. Correct when the rationale is genuinely historical.

Option 2 is the default worth writing down: a backlog file is by construction
*open work*, so a citation that survives the work is citing the wrong kind of
document.

## Cheapest mechanical half

`scripts/backlog-lint` already walks `docs/backlog/`. Add a reverse check:
grep the tree for `docs/backlog/[a-z0-9-]+\.md`, resolve each against the
directory, report the unresolved ones with their call sites. That makes the
debt visible and makes the next delete-on-ship notice immediately — which is
worth more than a one-time sweep of the current sixty, since the sixty will
otherwise regrow.

Sequencing: land the lint first (as a warning, not a gate), then burn down
what it reports in passing rather than as a project.

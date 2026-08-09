---
name: docs-triage
description: >-
  Triage docs/backlog/ — decide when an item file gets deleted (shipped /
  stale / snoozed), how far to compact one, and where surviving truth goes
  (the owning package docstring). Reach for it when scripts/backlog-lint
  flags done-marked items, when a ship leaves its backlog item behind, or
  when the backlog needs a currency pass. Repo-dev tool for developing
  precis-mcp; NOT a precis product skill.
---

# docs-triage — keep the backlog live, delete the rest

**The disease.** A `docs/backlog/*.md` item is *open work*. When its work
ships, the truth moves into code + the owning package docstring, but nobody
deletes the item — so it lingers, gets READ as open work, and rots. Cure:
**delete-default** ("rest in git for the archaeologists"). `git log` is the
history; `docs/backlog/` is the present. Contract: `docs/README.md`
§Backlog lifecycle.

## Start here

    scripts/backlog-lint     # flags done-marked items still in docs/backlog/

Advisory (never deletes, never fails a build); it surfaces candidates by
title-level done-markers only. You apply the judgment it can't. The
generated index (`docs/backlog/README.md`, rewritten by `scripts/docs-index`
at ship) is the one-line-per-item view.

## The per-item verdict

- **Shipped** — the work is on `main`. Verify (`git log`, the code), then
  **delete the file in the same commit** that shipped it — or now, if it was
  missed. Never leave a "DONE ✅" note; the backlog is the active list, not
  an archive.
- **Stale** — the item describes a problem the code no longer has, or a
  plan overtaken by a different shipped approach. Verify against the code
  (don't trust the prose), then delete. If deletion would orphan a claim a
  kept doc relies on, correct that doc to code-truth in the same commit.
- **Snoozed** — deliberately parked, not dead. `snooze-until: YYYY-MM-DD`
  front-matter; triage skips it until the date. On or after the date:
  re-probe the unblock condition, then act or bump the date.
- **Open** — keep. Compact if bloated (bar below).

Before deleting, re-scan for stragglers:

    grep -rn "<slug>" . --exclude-dir=.git    # fix every surviving ref

## The compaction bar

- An **idea** is ≤ ~15 lines: `# title` + what, why, owner anchor, `test:`.
  No front-matter needed.
- A **spec** (status `draft` → `ready`, per `docs/backlog/TEMPLATE.md`)
  keeps its full body while active — acceptance criteria and blast radius
  are load-bearing for the fixer; don't compact a live spec down to an idea.
- An idea that has grown spec-shaped sections without front-matter is a
  spec in denial — add the front-matter or cut it back to an idea.

## Where truth folds

On ship (or when cutting a stale item that carried real rationale), any
surviving truth — the "why", the rejected alternative, the invariant —
folds into the **owning package's `__init__.py` docstring**, compactly, in
the same commit. Nothing else is an archive: no done-log, no CHANGELOG, no
completed/ directory. Reusable incident forensics go to `docs/runbooks/`.

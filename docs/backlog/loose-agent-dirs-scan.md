---
status: draft
title: Periodic scan for loose agent working dirs (/tmp, ~/, old scratch) — reports, never reaps
pillar: platform
---

# Periodic scan for loose agent working dirs

Source gripes: gr520908.

Reto, 2026-10-26: file a meta-gripe each time an agent's working state
has no precis home and it falls back to loose files. The trigger: ~500
GRIPE_FIX fleet files in `/tmp` lost to the 2026-10-26 reboot
(gr519361), and a `design-tree` directory in the home dir holding a spec, a ruling log, red
lists, run metrics and ~25 scripts for two days before anyone mapped
them to homes.

## Motivation / why

When an agent's working state has no obvious precis home it writes loose
files. Nothing tells it this is wrong, nothing records that it happened,
and the files are lost on reboot or drift from the DB. Two of the
gripe's three asks already shipped: the file-type→home map and the
gripe-the-gap-then-the-meta-gripe convention live in
`precis-loose-files-help` (aa2fa6397); meta-gripes link gr520908 so the
count measures whether homes are closing. What remains is detection:
loose dirs that exist anyway are found by nobody until a reboot decides
for them.

## In scope

- A periodic scan that finds loose agent working dirs — `/tmp/<x>`,
  `~/<x>` trees written by agent sessions, worktree `scratch/` older
  than N days — and reports them to the owning session where that is
  derivable, else a board line.
- Each report names the directory, its age and file count, the
  session/agent that wrote it where derivable, and points at
  `precis-loose-files-help` as the resolution path.

## Explicitly NOT in scope

- The file-type→home map itself (shipped: `precis-loose-files-help`).
- The stale warning on unpicked gap gripes (gr520909).
- Deleting or moving files — the scan reports, it never reaps.
- Fleet work-state in the DB (gr519361; folded into
  `gripe-fix-loop-on-the-gripe.md`).

## Acceptance criteria

- A seeded loose dir (files older than N days, under no declared home)
  produces exactly one report naming the dir; a re-scan produces no
  duplicate until the report is acted on.
- A dir whose files all sit under a declared home (`scratch/` younger
  than N days, a worktree's tracked files) produces none.
- The report names where to take the files
  (`precis-loose-files-help`).

## Target + blast radius

Depends on where the scan runs (open question): a worker pass on each
host that runs agent sessions, a `scripts/` helper the coordinator
calls, or a SessionStart-adjacent hook. Read-only over the filesystem;
the only writes are an optional board line or gripe comment.

## Open questions / decisions log

- **Open:** who runs the scan — a worker pass on the fleet hosts (the
  fleet rows give the host list; it sees every machine, not only the
  laptop), a `scripts/` helper, or a hook. Recommendation: the worker
  pass.
- **Open:** what counts as agent-written — mtime plus a Claude/Codex/
  Devin marker vs any non-repo dir; and the age threshold N. The
  skill's stale window is 3 weeks; scan reporting should fire earlier,
  in days.
- **Open:** where the report lands — a board line, a comment on
  gr520908, or one meta-gripe per dir (self-filing, the skill's
  "every time" rule automated).
- **Decided (by ship):** gr520908 asks 1 and 3 are done —
  `precis-loose-files-help` carries the home map, the gap/meta-gripe
  rule and the gr520908 back-link (aa2fa6397).

---
status: idea
title: Fleet agents stall on prompts and lack a checkout — prod-psql approvals, review-note path, plan_tick repo access
pillar: platform
---

# Fleet agent friction — prompts and missing repo access

Bundled in the 2026-10-10 gripe triage. Each item stops fleet or lane
agents on something a human already decided or never needed to decide.

## 1. prod-psql hook asks on every write (gr462727, merged gr462731)

`guard-prod-psql.py` asks for confirmation on every prod write, including
writes Reto already approved. That contradicts
`docs/conventions/thresholds.md`, so approved prod work stalls on a prompt
in the pane. Classify writes (destructive, or over the threshold, asks;
the rest pass), or accept a per-approval token that the ask grants once.

## 2. Review notes under ~/.claude trigger "sensitive file" prompts (gr464238)

Fleet design and review notes live under `~/.claude`, so every thread
session's file-tool write to its review note prompts. Move the notes out of
`.claude` (e.g. into the repo's gitignored `.claude/fleet/` working dir, or
a non-dotdir), or add an allow rule scoped to that directory.

## 3. plan_tick has no repo checkout (gr477865)

plan_tick agents (`claude -p`) have no `PRECIS_PYTHON_ROOTS` and no worktree,
so they cannot check repo-dev memory-review todos against the code. The
options from the gripe are: map a read-only checkout into the lane, route
repo-touching todos to a lane that has one, or tag them `waiting-for:reto`.

Related: gr456287 / [worktree-path-guard-false-positives.md](worktree-path-guard-false-positives.md),
the other high-frequency prompt-friction source.

## Acceptance

- An approved non-destructive prod write runs without a prompt, and a
  `DELETE` without a `WHERE` still asks.
- A thread session writes its review note with no permission prompt.
- A repo-dev memory-review todo is either checked against the code by its
  lane or visibly parked for Reto. It is no longer re-ticked without
  progress.

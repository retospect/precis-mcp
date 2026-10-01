---
status: draft
title: The coordination session launches a thread's working session itself, from the thread file's launch brief
pillar: platform
prio: normal
---

# The coordination session launches a thread's working session itself

## Motivation / why

An active thread means a live Claude session in a worktree working it
(`docs/roadmap.md` §Active and dormant threads). Today only Reto can start
one: he types `claude -w <slug>` and pastes a brief. The coordination
session can run a headless `claude -w <slug> -p "<brief>"`, but that runs
one task to completion and exits — it is not a session that keeps owning a
thread, takes cross-session messages and answers rounds. At the 2026-10-01
pillar review four threads were activated (claims-and-evidence,
se-machine-design, chemistry, local-compute) and each waited on Reto to
launch it from a brief kept in `/tmp`. Reto, 2026-10-01: "I like it."

## In scope

- Launch brief moves into the thread file: a `## Launch` section (the
  first message a new owner session gets), so it is versioned and not lost
  in `/tmp`.
- `scripts/launch-thread <slug>`: refuses unless the thread is listed
  Active in `docs/roadmap.md` and no live session already holds the
  worktree (`scripts/inflight --json`); then starts an interactive
  `claude -w <slug> "<brief>"` detached in a terminal multiplexer (tmux is
  not installed on the Mac today — add it, or use the Claude desktop/remote
  session if that proves the better host), and prints how to attach.
- A cap on concurrently launched sessions (gate slots are 2 and each
  session bills), read from one setting.
- The coordination/deploy session may call it for Active threads without
  asking; launching a Dormant thread stays Reto's word.

## Explicitly NOT in scope

- Changing any session's permission mode or settings.
- Unattended restart loops (a crashed session is relaunched by a human or
  by an explicit later item).

## Acceptance criteria

- `scripts/launch-thread chemistry` with chemistry Active and unheld starts
  a session that is visible in `scripts/inflight` with its purpose line
  written, and that receives a SendMessage.
- The same call with the worktree already held, or for a Dormant thread,
  refuses and says why.
- Every Active thread file carries a `## Launch` section;
  `scripts/backlog-lint` flags an Active thread without one.

## Target + blast radius

`scripts/launch-thread` (new), `docs/backlog/threads/README.md` (the Launch
section), `scripts/backlog-lint`, the active thread files. Owner: the
factory thread.

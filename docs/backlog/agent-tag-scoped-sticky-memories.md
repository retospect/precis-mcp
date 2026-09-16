---
status: draft
title: Agent-tag-scoped sticky memories — one memory store, per-agent identity slices
prio: normal
---

# Agent-tag-scoped sticky memories

Split out of the soul-store assessment 2026-09-16
(`soul-splay-prime-adoption.md`) as the one piece worth doing
independently.

## Problem

asa's preamble tier 3 pulls sticky (pinned) memories, keyed per *user*
via the `author_handle` tag pattern (`src/asa_bot/preamble.py`). As
more agents share the memory kind (asa Discord, asa_slack, future
personas), there is no per-*agent* scoping: every agent surfaces the
same pinned identity/behavior memories. One store should serve N
personas with distinct initial slices.

## Proposal

Scope sticky-memory retrieval by an agent tag, the same mechanism as
the existing per-user tag key:

- Each serving surface passes its agent identity; tier-3 retrieval
  filters pins to `agent:<name>` plus untagged/shared pins.
- Tag as **rerank bias, not hard gate**, or at minimum always include
  untagged pins: a hard gate makes an untagged memory invisible to
  every agent's startup, and write-time tag discipline becomes
  load-bearing. Bias degrades gracefully.
- No schema change: existing open tag axis on `memory`.

## Open questions

- Whether shared pins are the untagged set or an explicit
  `agent:shared` tag (explicit is auditable; untagged-as-shared is the
  graceful default — probably both: explicit shared tag honored,
  untagged treated as shared).
- Where the agent name enters `preamble.build(...)` — likely alongside
  `platform`, which already distinguishes Discord/Slack.

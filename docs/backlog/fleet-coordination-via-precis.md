---
status: idea
pillar: platform
---

# Fleet coordination via precis

Idea, not specced. The 2026-10-07 token-review pass measured coordination
chatter at about 10% of fleet cache reads (~55M tokens in 24h): the
coordinator took ~180 message turns (72 peer messages in, 112 `SendMessage`
out) and the gripe loop 41 `ScheduleWakeup` ticks. Each costs a full turn at
the receiver's ~250k context, whatever the message says. Reto's ruling: fine
for now, worth considering later.

Direction to consider: move the status relay into precis state that sessions
read when they choose to, instead of messages that wake them — for example
round status and per-thread purpose lines as precis refs, written by
`scripts/round in|none|eta` and read with one `get` at a natural break. Keep
direct messages for things that need an answer now.


## Measurement (2026-10-08, coordinator transcript of 10-07)

72 inbound messages, hand-bucketed (±5): status 37 (51%), rulings/orders
relayed from Reto 18 (25%), questions/blockers 10 (14%), cross-machine relay
7 (10%; 6 from the Codex fleet on melchior). Coordinator turns after them:
64.4M cache reads, upper bound — status 30%, rulings 40%, questions 19%,
relay 10%. 23 of the 37 status messages repeated a `scripts/round` mark the
sender had already written. Gripe loop: 41 `ScheduleWakeup` calls were
mostly re-arms inside message-woken turns; ~5 timer fires, ~2 noop.

So: most status chatter is senders messaging *after* marking — a prompt
fix, no store needed. That fix is in (round-open message, AGENTS.md
§Rules by pointer, runbook codex-fleet); re-measure a round's coordinator
transcript before building anything here. A shared store removes the 10% relay only if the
melchior clone writes to it too. Rulings and questions stay messages.

## Why it is more than a token saving (2026-10-08)

- **Cost is small in dollars.** 55M cache reads ≈ $11/day at API rates
  ($0.20/MTok on Opus 5.5 and Sonnet 5.5); on Max it is plan quota. Not a
  reason to build on its own.
- **Round state does not cross machines.** `scripts/round` keeps it in
  `<git-common-dir>/precis-round/round.json` (`_state_dir`), one per clone.
  Codex workers on melchior and Claude sessions on the Mac cannot read each
  other's marks; messages are the only shared channel. State in the prod DB
  is visible from every node.
- **Vendor-neutral.** `codex queue` is Codex-only and `SendMessage` is
  Claude-only. Every pool that speaks MCP can `get`/`put` — Devin CLI on the
  Mac already has the precis MCP wired.

## Shape if the measurement says build

1. **Store.** One ref per open round (each peer's mark) and one per worker
   (purpose line, claim). Prefer an existing kind (tagged `todo`, or
   `memory` in a fleet space); a new kind also needs `test_kind_totality` +
   `test_item_view` and a migration.
2. **Dual-write, then flip.** `scripts/round in|none|eta|open` and the
   purpose write also write through `scripts/prod-precis tools …`; `round
   status` reads precis with `round.json` as fallback. Flip after a few
   rounds. Release/deploy logic in `scripts/round` stays untouched.
3. **Readers and prompts.** Coordinator does one `get` at natural breaks;
   Codex prompts and AGENTS.md route status to precis, messages only for
   what needs an answer now.

Fallback when prod precis is down: `round.json`. Write-path tests on the dev
DB.

## Beyond messaging: one view, two stores

Pillars (`docs/roadmap.md`), threads and backlog items stay in the repo:
they change in the same diff as the code, under review and the gate. Gripes,
todos, Reto's queue and quests stay in precis. The friction is link rot
between the two — orphan gripes relinked by hand at pillar reviews, stale
backlog cites, claims split between a `wip:` tag and `.claude/purpose`, no
single "in flight / owner / blocked on" query. Options, not decided:

- precis indexes the repo layer read-only (frontmatter: slug, status,
  pillar, thread) so `link` joins a gripe or todo to a backlog slug.
- A gate check fails when an item or thread cites a closed or missing
  gripe/todo id, and lists gripes no item points to.

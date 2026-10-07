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

Before speccing, measure the split: how many of the coordinator's inbound
messages were pure status (no action needed) versus questions or blockers.
See `session-prefix-and-fleet-context-cost` (c) for the measurement.

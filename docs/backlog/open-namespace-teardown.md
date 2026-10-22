---
status: idea
pillar: knowledge
---

# Retire the free-form `OPEN` tag namespace

Reto: "nuke OPEN* soonish". `OPEN` holds three things: machine control
tags, staging for curated axes (ADR 0047) and folksonomy. The machine pile
is migrated (2026-10-10: ~10 machine-prefix rows left); ~10,250 `OPEN`
tags remain, ~1,100 of them `topic:`, plus junk (`status:exercise-*`,
shadow `prio:high/low`, `projects:` typo, opaque `kind:a1`).

- **Consolidate** `topic:`/`interest:` into curated axes or taxa. The
  2026-10-22 ontology review (memory me504796, gripe gr504795) found ~25
  `interest:`/`topic:` synonym pairs and proposes promoting campaign tag
  vocabularies into taxa — plan this item from that review.
- **Delete** the junk pile.
- **Traps:** cull with exact `namespace='OPEN' AND value LIKE 'p:%'`,
  never `namespace LIKE 'OPEN%'` (eats `OPEN-QUESTION`); `severity:` ≠
  `SEV`, `prio:` ≠ `PRIO` ≠ `refs.prio`; `sticky:` has no writer but memory
  TTL reads it — don't cull.
- **Open for Reto:** `level:` as axis or column; the `internal-thought`
  dual writer; `sticky:`'s fate.

Source: memory me474399; the design doc shipped in b02ad7e3c and was
deleted on ship. Filed on Reto's go 2026-10-22.

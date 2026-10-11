---
status: idea
title: The quest DFT lint rule "not a slab" is per quest type, so azobenzene linear-motor quests pass
pillar: quests
---

# DFT lint per quest type

From gr415941 part 5 (split out 2026-10-11, see
[briefing-morning-report-quality.md](briefing-morning-report-quality.md)).

The DFT lint rejects azobenzene linear-motor quests with "not a slab". The
check assumes every quest is a surface-catalysis slab quest. Reto ruled to
make the rule per quest type: slab checks apply to slab quests only, and
other quest types get their own checks or none.

Find the check by searching the quest lint code for the "not a slab" message.

Who: agent-code. S.

## Acceptance

- An azobenzene linear-motor quest passes DFT lint; a slab quest with a
  non-slab seed still fails.

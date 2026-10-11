---
status: idea
pillar: memory-graph
title: Ingest a paper from a Discord attachment
---

# Paper front door: Discord attachment

Left from the 2026-10-11 gripe triage (citation string and un-indexed DOI
shipped: `put(kind='paper', citation=…)`, `precis-paper-help`).

## Attach a PDF in Discord for ingestion (gr434187)

`src/asa_bot/` has no attachment handling, so a PDF posted in Discord cannot
reach the ingest pipeline. Needs a design first: which channel/identity may
ingest, size and MIME limits, where the file lands (the ingest watch
folder versus a direct `ingest_pdf` call), and what the bot replies with
(the `pa<id>`). The URL fetch rules in `docs/conventions/invariants.md` do
not cover Discord CDN URLs; decide whether to go through `safe_stream`.

Ruling (Reto, 2026-10-11): wanted; go ahead. Default to Reto's own
Discord identity (the asa account mapping) and the channels asa already
serves; reply with the `pa<id>`.

Who: agent-code, M (design the limits above in the PR description).

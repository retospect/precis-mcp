---
status: idea
pillar: memory-graph
title: Get or ingest a paper from any handle a human has — citation string, un-indexed DOI, Discord attachment
---

# Paper front door by any identifier

Bundled in the 2026-10-11 gripe triage. A human holds a paper as a citation
string, a DOI, or a PDF in hand. Each should reach a paper ref without the
agent hand-assembling it.

## 1. Classic citation string to DOI (gr437652)

`get`/stub of a paper from "journal vol page year" (e.g. *Phys. Rev. B 47,
558 (1993)*) has no resolver. Fix direction: parse the string, query the
Crossref works API with journal, volume, first page and year through
`safe_get`, accept a single unambiguous hit, and feed the DOI to the normal
`paper` get/stub path. Ambiguous or empty results return the candidates, not
a guess. Owner `src/precis/handlers/paper.py` (+ a small Crossref helper next
to the existing Crossref callers).

Who: agent-code.

## 2. Get a paper by an un-indexed DOI (gr414666)

The skill says `get(kind='paper', id=<DOI>)` fetches a DOI that is not yet
indexed; the gripe reports it did not. Verify first on
`10.1126/sciadv.aeh2779`: if the call mints/fetches the paper, close the
gripe; if not, fix `src/precis/handlers/paper.py` so it does, or correct the
skill text in `src/precis/data/skills/`.

Who: agent-code (S, verify then fix or close).

## 3. Attach a PDF in Discord for ingestion (gr434187)

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

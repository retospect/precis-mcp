---
status: draft
pillar: memory-graph
title: Dreaming classify-and-rehome — research-space writes get a kind, a space and a parent at the batch boundary, rules first
prio: normal
---

# Dreaming classify-and-rehome — type the research-space writes at the batch boundary

## Motivation / why

Counted 2026-11-01: 12,313 live memories, of which SPACE:research 739 and
SPACE:repo-dev 534 — about 11,000 carry no SPACE tag, no type and no
parent. The repo-dev 534 are the only structured part of the graph (hubs,
`section:` types, `part-of`, lint). The research side has many writers
(quest ticks, the dreaming worker's `tier:synthetic-insight` nodes, asa
conversation notes, `card_forge`) and five destination kinds — `finding`
(single-source claim), `memory` (lemma/inference/note), `concept`, `anki`,
`taxon` — with nothing enforcing the choice. gr547900 asked for a learned
router on the put path; the 2026 evidence says the opposite: typed
separation matters (collapsing typed stores cost ~30 points in one
ablation), but an LLM pass per write is the expensive, lossy way to get it
(eager per-exchange construction: 4× construction tokens and −7 points vs
batching at semantic boundaries). The batch boundary already exists here —
the dreaming worker is the research space's consolidation pass and already
merges near-duplicates (`handlers/memory.py`, `_DREAM_CONSOLIDATED`).

## Target shape

Each research-space memory, after one dreaming pass, carries: a `SPACE:`
tag; a kind that matches its content (rehomed if not); a `part-of` edge to
its parent — the quest it was written under (`quest-graph-as-dossier.md`:
the quest node is the apex), else the conversation/topic hub asa writes
under; and a type tag from a small research axis (`section:` reuse:
`reference`, `gotchas` → `caveats`, `threads` → `open`, plus `insight` for
dreaming output). The repo-dev lint checks (no hub, orphan, eye-truncated,
cold) then run over the whole graph, not one space.

## In scope

1. **Classify step in the dreaming pass** (`src/precis/workers/` dreaming
   job): for each memory touched since the last pass with no SPACE or no
   parent, apply rules in order, no LLM:
   - cloze markup `{{cN::` → `anki`;
   - exactly one `[pc…]`/`[pa…]` cite and a claim-shaped first line →
     `finding` (mint through taproot's existing path, link `derived-from`
     the memory, soft-delete the memory with `supersedes`);
   - definition-shaped (`X is/are …`, ≤40 words, no cite) → `taxon`
     (dedup-on-put decides new vs existing; link the memory `same-as`);
   - else stays `memory`.
   Each rehome writes a `ref_events` row with the rule name and the
   original kind, so the override rate per rule is countable.
2. **Parent step:** quest link from the writer's context (the tick's
   quest id; asa's conv handle); a node with neither goes to a per-space
   "unplaced" hub the lint reports on.
3. **Ambiguous residue:** rule conflicts or no rule fire → a Sonnet-tier
   batch call (one call per 50 nodes, not per node) proposes kind + parent;
   the proposal is applied only when its confidence tag is `high`,
   otherwise it lands as a `memory-review` todo. The batch is the only LLM
   cost in the design.
4. **Override log → `dry_run` parity:** the same rule set backs
   `put(kind='memory', dry_run=True)` (gr548271) so new writes see the
   proposed kind before writing; the rehome log and the dry-run suggestions
   are the dataset that would justify a learned router later — nothing
   here trains one.
5. **SPACE backfill** is gr548275 (one-off CLI with a dry-run report); this
   item keeps the space current afterwards.

## Explicitly NOT in scope

A learned classifier on the put path; changing the repo-dev write path
(the `remember` skill + `Next:` lines already enforce structure there);
inventing new kinds; rewriting node bodies (rehome moves, it does not
rephrase); the quest apex relation decision itself (`quest-graph-as-dossier.md`
Q1 — this item consumes whatever edge that decides, `serves` until then).

## Acceptance criteria

- After one pass on a fixture of 200 untyped research memories, every node
  has a SPACE, a parent (or the unplaced hub) and a type tag; the rule
  table in the pass log sums to 200.
- Rehome override rate is readable per rule from `ref_events` (a count
  query, or the gr548280-style count view).
- `scripts/memory-lint` run with `--space research` reports no "no hub"
  findings on the fixture after the pass.
- The LLM cost of the residue step is one call per 50 nodes, logged in
  `llm_call_log`.

## Target + blast radius

`src/precis/workers/` (dreaming job), `src/precis/handlers/memory.py`
(shared rule module with gr548271), `src/precis/taproot/` (finding mint
path, read-only use), `scripts/memory-lint` (space flag). No migration.

## Open questions / decisions log

- 2026-11-01: rules-first, LLM only for the residue in batches — decided
  from the gr547899/900/901 review (closed into this and the gripes
  above).
- Open: the research type axis — reuse `section:` with the mapping above,
  or a separate `rtype:` axis. Blocker-severity: no (default: reuse
  `section:`, the lint already keys on it).
- Open: whether asa's conversation notes get a per-conversation hub or a
  per-topic hub. Default per-conversation (the conv handle exists today).

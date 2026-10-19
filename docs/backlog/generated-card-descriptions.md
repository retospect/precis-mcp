---
status: in-progress
title: Generated card_description chunks for non-text kinds
pillar: memory-graph
---

# Generated card_description chunks for non-text kinds

Gripe gr497688. Structures, PCBs, CAD designs and figures carry
geometry/binaries plus a terse template card (`card_combined`:
composition + counts + an optional agent-supplied description), so
hybrid search finds them only by filename-like text. The CLaC paper
(pa497686, DOI 10.1038/s41467-026-76378-7) shows synthetic,
property-conditioned narrative descriptions outperform literature text
for retrieval alignment — the same fix applies here: an LLM writes a
narrative description per ref, stored as a new `card_description` chunk
kind (`ord=-1`) that the embed worker picks up and the kind's
`card_kinds` admits into search.

## Reto's rulings (gr497688 comment 2, 2026-10-19 ~12:0xZ)

1. **Ongoing generation runs on Sonnet** (router `Tier.BIG`). The
   backfill is a separate path: Devin SWE-2 High agents write
   descriptions through the MCP, so backfill costs nothing against the
   $25 rule.
2. **Figures are described from the image itself** (vision input), not
   only from caption or text.
3. **A description is regenerated when its record is edited**, by
   replacing the one existing derived chunk — DELETE+INSERT of the
   single `card_description` chunk in one transaction (the
   `store/_chunks_ops.py::_replace_card_combined` pattern), never an
   in-place UPDATE, never more than one per ref.

## Slices

- **S1 — structure** (worktree `d-desc-s1`): the worker
  (`workers/card_describe.py`, modelled on `workers/chunk_keywords.py`),
  the `card_description` chunk_kinds migration, and
  `handlers/structure.py`'s `card_kinds` gaining `card_description`.
  Grounding: `handlers/structure.py::card_text` (the live
  `card_combined` chunk text is its rendering). Selection: structure
  refs with no `card_description`, or whose card text hash differs from
  the stored `meta.source_sha`. The claim is bounded to cards minted
  after the feature landed (the `chunk_kinds` row's `created_at` is the
  deploy stamp) so the worker never sweeps the existing ~2.4k-structure
  corpus — backfill is S4's Devin-agent path per ruling 1.
- **S2 — cad + pcb**: same worker shape over `CadHandler._card_text`
  and the pcb card builder (locate it — comment 1 left it open).
- **S3 — figure**: vision input per ruling 2; the raw SVG stays
  `no_index`.
- **S4 — backfill + budget cap**: the Devin-agent MCP write path for
  existing refs, plus a cost cap with a dry-run count on the ongoing
  Sonnet generation.

## Explicitly NOT in scope (S1)

- cad / pcb / figure kinds (S2, S3).
- Backfilling existing refs through the worker (S4; also why the claim
  carries the minted-after watermark).
- Embedding the description — that is the embed worker's job.
- A second description per ref, or ever UPDATE-ing chunk text.

## Acceptance criteria

- A structure ref with no `card_description` gets exactly one after the
  pass; a re-run is a no-op; an edited card regenerates (still one).
- `search(kind='structure')` finds a ref by a phrase that appears only
  in its generated description.
- The worker makes no embedder calls.
- `tests/test_dispatch_args_keys.py` stays green; no prod writes in
  tests (dev DB only, fake LLM injected).

## Target + blast radius

`src/precis/workers/card_describe.py` (new), `workers/registry.py`,
`cli/_worker_passes.py`, `store/_chunks_ops.py` (generalised
card-replace helper), `handlers/structure.py` (`card_kinds`), one
migration registering `chunk_kinds.card_description` — which routes the
slice through the release owner, not a builder ship.

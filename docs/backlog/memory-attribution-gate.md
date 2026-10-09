---
status: in-progress
pillar: memory-graph
title: memory attribution gate — a number pinned to a citation must appear in the cited text; warn, then reject; retro-apply over the graph
prio: high
model: sonnet
---

# Memory attribution gate

**State (2026-10-09):** §1–§5 shipped in warn mode. Remaining: run
`scripts/memory-attribution-audit` dry against prod, read the sample, then
flip `PRECIS_MEMORY_ATTRIBUTION_GATE` default to `reject` and `--apply` the
backfill. Delete this file in that commit.

## Motivation / why

Reto audited a random prod memory on 2026-10-09 (me170351, a
`DREAM:speculative` dream memory). Its synthesis was fine and correctly
tagged speculative. One clause was not: *"ℓc (~10 nm for stiff solids per
websearch:170350)"*. The cited websearch cache contains no numeric scale
at all; the number was the model's recollection, pinned to a source that
never said it. The figure then propagated into me170353 as a concrete
test target ("reproduce the ~10 nm scale from websearch:170350").

The distinction that matters: **`DREAM:speculative` licenses unsourced
claims, not mis-sourced ones.** A reader discounts the synthesis because
of the tag but trusts the cited number, because citations are the thing
the tag says you can still rely on. An unattributed "roughly 10 nm, my
own estimate" would have been fine and auditable.

Today nothing checks this. Every memory, dream memories included, is
created through `MemoryHandler._create` and edited through
`MemoryHandler._write_body` (`src/precis/handlers/memory.py`); neither
validates content. The dream worker (`workers/dream_agent.py`) does not
write memories itself — the dream LLM calls `put` over MCP with
`edit`/`delete`/`link` blocked — so a handler-level gate covers dream
writes without touching the worker. The only text-level hook today is the
advisory first-line nudge.

Scope in prod (2026-10-09, read-only count): 11,950 live memories; 10,487
cite a source; **2,994** cite a source *and* carry a unit-bearing number
somewhere in the body (upper bound — the proximity check timed out in SQL
and runs in Python below).

## What ships

### 1. `ungrounded_cited_numbers()` — pure, deterministic, no LLM

New module `src/precis/handlers/_attribution.py`:

```python
@dataclass(frozen=True)
class UngroundedNumber:
    token: str        # as written, e.g. "10 nm"
    cite: str         # the citation token it sits next to, e.g. "websearch:170350"
    ref_id: int | None  # resolved target, None when the cite does not resolve

def ungrounded_cited_numbers(store: Store, body: str) -> list[UngroundedNumber]: ...
```

Algorithm:

1. **Citation spans.** `finditer` with the autolinker's own regexes from
   `utils/mentions.py` (`REF_PATTERN`, `DRAFT_MARKUP_PATTERN`,
   `BARE_PAPER_PATTERN`) **plus** bare 2-letter-code handles
   `\b(pa|pt|me|fi|pc|pk|fb)\d{3,}\b` — dream prose writes `pc995663` and
   `pa5835` unbracketed, and the audited memory's links prove those forms
   are what the corpus contains. Resolve each span to a ref (and chunk,
   for `pc`/`pk`/`~N`) through the existing `resolve_handle_ref` /
   `resolve_handle_target` / `handle_registry.parse`. Unresolvable
   citations are reported with `ref_id=None` and are **not** a grounding
   failure (that is a different defect).
2. **Window.** The sentence containing the span, widened to at most 200
   characters each side and clipped at a blank line.
3. **Numbers in the window.** `utils.numerics.extract_numerics` tokens
   (unit-bearing values and percentages) — nothing else. Years, ordinals,
   equation and figure numbers, exponents of `r²`/`r³`, and bare counts
   never fire. The number inside the citation token itself is excluded.
4. **Evidence.** For a chunk-level cite: that chunk's text and its
   neighbours ±1. For a ref-level cite: `SELECT unnest(numerics)` over
   the ref's chunks (cheap for a 400-page textbook) plus the ref title;
   for websearch / perplexity / web / memory targets, the body chunks'
   text. Both sides go through `taproot/reword._canon_grounding`;
   grounding decided by `taproot/reword._is_grounded` (decimal-prefix
   tolerance, integers exact). Import those two helpers; do not copy them.
5. **Exemption phrase.** A number within 60 characters *after* which the
   text says `my estimate`, `my own estimate`, `(est.)`, `(estimate)` or
   `rough guess` is exempt. This is the escape hatch the nudge tells the
   writer to use.

### 2. Handler hook — warn now, reject later

- `MemoryHandler._create` and every `_write_body` caller compute the
  misses on the final body. On any miss:
  - add the closed tag **`AUDIT:ungrounded-number`** (set_by `system`);
  - append an advisory block to the ack, one line per miss:
    `ungrounded: "10 nm" near websearch:170350 — not in the cited text; drop the attribution or write "(my estimate)"`.
- A later clean write **removes** `AUDIT:ungrounded-number` (the gate is
  derived, like `STALE:retracted-premise`).
- Mode switch `PRECIS_MEMORY_ATTRIBUTION_GATE=warn|reject`, default
  `warn`. In `reject` the write raises `BadInput` with the same lines and
  a `next=` telling the writer the two fixes. The default flips to
  `reject` in a follow-up commit after the backfill report (§4) shows a
  false-positive rate Reto accepts.
- `review._write_digest` (`workers/review.py`) writes memories around the
  handler with the body in `refs.title`; it carries no citations today, so
  it is out of scope here but listed under follow-ups.

### 3. Tag axis and rendering

- `store/types.py`: add `"ungrounded-number"` to `_CLOSED_VOCAB["AUDIT"]`;
  add `"AUDIT"` to `_KIND_ALLOWED_AXES["memory"]`. Agents may also set it
  by hand (an auditor wants to), so it stays agent-writable like on `todo`.
- Memory header render (the plain `get(kind='memory')` and the
  fisheye ring label) shows the AUDIT tag on its own line so the next
  reader sees the verdict before the prose.
- `data/skills/precis-tags.md` AUDIT row and the `memory` row;
  `precis-memory-help.md` gets a short "numbers next to citations" note.

### 4. Backfill — the gate retro-applied

`scripts/memory-attribution-audit` (stdlib + precis store; prod via the
same connection path as `scripts/prod-psql`, dev via `scripts/dev`):

- Candidate set: live memories whose body matches a citation token and a
  unit-bearing number (the 2,994-row SQL from the count above, reused).
- Runs `ungrounded_cited_numbers` per memory in Python.
- Default is **dry-run**: prints counts (candidates / flagged / per-cite-kind
  breakdown) and a 30-row sample `me<id>  "<token>"  <cite>` for a human
  false-positive read. `--apply` adds the tag. No body edits, no LLM, in
  this item: fixing the prose is the dream agent's or an auditor's job
  once the tag points at it.
- Must finish in minutes, not hours: batch the evidence lookups per
  target ref (many memories cite the same papers).

### 5. Dream prompt

`src/precis/data/prompts/dream-prompt.md` Step 6, after the paragraph that
ends "name the fact-bearing leg explicitly": one rule — *a number written
next to a citation must appear in the cited text; if it is your own
recollection or estimate, say so inline ("~10 nm, my estimate") and do not
attach it to the handle.* Already applied in this tree.

## Tests

- Unit (`tests/test_memory_attribution.py`, no DB): grounded number next
  to a cite passes; same number absent fails; decimal-prefix tolerance;
  integer exact; exemption phrase; number inside the handle ignored;
  bare `pc123456` and bracketed `[pa12]` and `websearch:170350` all
  detected as cite spans; two cites in one sentence attribute the number
  to the nearest.
- Handler (`tests/test_memory.py` additions, dev DB): create with a
  mis-sourced number → tag present + nudge line; `reject` mode → BadInput;
  a follow-up clean edit removes the tag; agent `tag(add=['AUDIT:ungrounded-number'])`
  accepted on memory.
- Script: dry-run over a seeded dev DB prints the sample and changes
  nothing; `--apply` tags exactly the flagged rows.

## Not in this item (follow-ups, keep as pointers)

- gripe 476896 — `get(kind='memory', view='fisheye+1hop')` raises
  `KeyError` when the ring holds websearch or paper refs; the recall view
  every session is told to use.
- `DREAM` is documented as system-set but is agent-writable
  (`_mappers.py` lists it in neither prefix set); `DREAM:grounded` is used
  by `workers/axis_pass.py` but is not in `_CLOSED_VOCAB`.
- `review._write_digest` bypasses `MemoryHandler` (body in `refs.title`,
  no mention autolink, so no gate).
- Memories cite websearch caches by numeric id (`websearch:170350`) but
  the kind's `get` handle is the slug; the citation as written cannot be
  fetched. Either accept numeric ids in `get(kind='websearch')` or print
  the slug in the `cite as` footer.
- LLM-assisted triage of the flagged backfill rows (reuse
  `workers/_chase_llm._verify_support_with_caveats`) and automatic
  rewrite of the attribution phrase — only if the deterministic tag
  turns out to leave too many rows for humans.

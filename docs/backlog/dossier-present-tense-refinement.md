# Dossier: present-tense document, incremental refinement

> **Status:** design, agreed with Reto 2026-08-13. Slices 0–3 (pinned-chunk
> hygiene, bracket handles, prompt house style, ledger→chunks+`ATTEMPT:`) ship
> separately and independently; this doc specifies the slice that changes the
> **tick contract** itself.

## The contract

The dossier holds **best current understanding, the outlook, and the hypothesis
under test — with reasons and cites.** That is a *present-tense* document. It
describes what we believe now, not the journey that got here.

Everything else has a home already and does not belong in the body:

| | lives in | today |
|---|---|---|
| what we **thought** | chunk edit history (`prev_text`) + the ledger's do-not-re-propose trail | already exists, free |
| what we **got** | links to the `structure`/`finding` refs that hold the measurement | derivable; `precis/quest/frontier.py::render_frontier_tree` already does it |
| what we **want next** | the hypothesis under test — the only genuinely volatile prose | smeared through the narrative |

This is what keeps the document small **by construction**. Terseness is not a
retirement discipline bolted on afterwards; it falls out of the document being
about *now*.

## Why the current design cannot get there

`precis/quest/dossier.py::rewrite_dossier` replaces the whole narrative chunk
every tick (`store.edit_text(body[0].handle, markdown)`), driven by a single
`dossier_text` field in the tick response
(`precis/quest/tick.py::_PROMPT_TEMPLATE`).

A document regenerated wholesale each cycle **cannot carry structure**: no chunk
keeps a stable handle, so nothing can be linked to, tagged, reviewed per-chunk,
or cited from elsewhere. Every tick throws the graph away and rebuilds prose
from scratch. That — not the markdown syntax — is why the dossier is a blob.

### The system already objected to this, correctly

In Aug 2026 an automated structural/hygiene review flagged dossier 202546 with
*"This is written with markdown inside a draft. Refactor it to use draft
chunks."* — the same diagnosis a human made independently. A `plan_tick` agent
executed that todo against the live dossier: it retired the narrative chunk and
the pinned ledger chunk and re-added the content as 18 typed chunks. Its summary
("no content was lost, only the formatting structure was normalized") was
locally true and systemically wrong.

The systemic damage was **not** the ledger loss, which turned out to be mild:
the retired ledger held 184 characters (two `[open]` bullets) and the
self-healed replacement re-accumulated past it within days. The damage was that
the refactor **stranded 12 chunks** the writer no longer addressed. Because
`rewrite_dossier` wrote only `body[0]` while `read_narrative` read *all*
unpinned body chunks, the next 16 ticks were fed 8 chunks frozen at 2026-08-10
under the banner "the living synthesis". (The "Pareto frontier: Empty" line in
that stale block was *not* itself wrong — quest 202469 genuinely has zero
candidate structures; see
`docs/backlog/quest-frontier-tree-seed-indistinguishable-from-empty.md`.)
A structural edit to a machine-owned document silently corrupted
the *input* to the process that owns it, and nothing detected that for two
weeks. (The read/write asymmetry itself is now fixed in
`precis/quest/dossier.py::read_narrative`, which reads `body[0]` and logs a
warning if it ever sees more than one.)

Two guards now exist (a refusal at the draft handler boundary, an exclusion in
the scanner), but they only suppress a *correct* complaint. The hygiene
heuristic is right: a dossier really is markdown stuffed in a draft. This
redesign is what makes the complaint stop being true, rather than stop being
heard.

The two obvious alternatives are both wrong:

- **rewrite everything** (today) — destroys handles, and re-derives unchanged
  understanding from scratch every cycle.
- **append a chunk per tick** — turns understanding into a transcript. Grows
  monotonically; the reader has to reconstruct the current belief by replaying
  history.

## The third mode: work the new learning in

The tick's job each cycle is to **refine the existing document in place**.
Usually that is a few words on one paragraph. Sometimes it *shortens* the
document, because new evidence collapses an uncertainty:

> before: "unsure whether a or b dominates, since foo or bar may be the
> limiting factor"
> after: "b beats a by x [st164913]"

Shorter **and** strictly more informative. This is the case that proves
understanding is not monotonic in length, and the reason a word-count growth
ratchet is the wrong instrument.

This mode is more work per tick than either alternative — the model must decide
*which* paragraph a new result belongs in — but it is the only one consistent
with a present-tense document.

The write affordance already exists and needs no new store API:
`edit(kind='draft', id='dc<id>', find=…, text=…, base_sha=…)` — anchored edits
with optimistic concurrency on `base_sha` and a `dry_run` preview
(`precis/handlers/draft.py::DraftHandler.edit`).

## The gate: cite-diff, not word count

Replace the whole-narrative growth ratchet
(`precis/quest/narrative_budget.py`, applied at
`precis/quest/tick.py::_apply_narrative_gate`).

**Every refinement edit must carry the evidence handle that caused it.** If a
paragraph changed and no `[st…]` / `[pc…]` / `[pa…]` / `[fi…]` handle appeared,
resolved, or moved in that edit, the edit is restatement — bounce it.

Why this is the right instrument:

- Word count cannot distinguish restated history from new evidence. A cite-set
  diff can.
- It cannot be gamed by shortening (the current ratchet only punishes growth).
- It implements "with reasons and cites" as a mechanical check rather than a
  hope.
- It doubles as fabrication defence: "cite deeply" is exactly the instruction
  that makes models invent handles. Gate on the **unresolved**-handle count as
  well — the grammar already flags unresolved handles on a verbatim read
  (`precis/utils/mentions.py::BARE_BRACKET_REF_PATTERN` +
  `precis/data/skills/precis-draft-help.md` "never fabricate a handle"), so
  fabrication becomes caught rather than trusted.

## Prompt discipline

The failure mode of in-place refinement is **smearing**: one finding worked into
three different paragraphs because the model could not decide where it belonged.

Design against it by making the tick **name its target before it writes** —
target chunk handle + the evidence handle motivating the change — as structured
fields, not free prose. An op that cannot name both is not a refinement.

Full rewrite stays available but is demoted to an explicit **transition**
operation (quest phase change, direction abandoned, reset), not the per-tick
default.

## What this makes cheaper

An earlier draft of this design worried that a many-chunk dossier would outgrow
the tick prompt around tick 50 and need a programmatic fisheye into its own
dossier (the affordance smartdraft already gives humans:
`precis_web/smartdraft.py::assemble_view`).

The present-tense contract largely removes that cost — a document about *now*
does not grow to 200 chunks, so the tick can still hold it whole and only needs
to pick which chunks to refine. Revisit only if real dossiers drift past what a
prompt can carry.

## Open questions

- **What counts as a "transition"** that licenses a full rewrite? Needs a
  concrete trigger list, or the escape hatch becomes the default again.
- **Cite-diff gate on the first tick** — a fresh dossier has no prior cite set;
  the gate must not deadlock a dossier that has nothing yet.
- ~~**Does the ledger stay a separate pinned chunk**~~ — **answered by slice 3.**
  It became one chunk per attempt node, `meta.pinned='ledger-node'`, nested via
  `parent_chunk_id`, with status on a closed `ATTEMPT:` axis. Still excluded
  from the narrative body by the existing `meta.pinned`-truthy filter, so it
  keeps the "survives every rewrite" guarantee for free while being individually
  addressable. `read_ledger` re-projects markdown on the fly, so callers that
  wanted text did not change.
  → follow-on defect: `docs/backlog/quest-ledger-accumulates-duplicate-branches.md`
- **Interaction with per-chunk review** — many small chunks each acquire a
  review state (`precis_web/smartdraft.py::review_indicator`). Confirm the
  review fan-out cost stays sane at ~20 chunks/dossier × 14 dossiers.

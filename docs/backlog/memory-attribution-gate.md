---
status: in-progress
pillar: memory-graph
title: memory attribution gate — a number pinned to a citation must appear in the cited text; warn, then reject; retro-apply over the graph
prio: high
model: sonnet
---

# Memory attribution gate

**State (2026-10-10, fifth dry run):** §1–§5 shipped in warn mode
(b88ae940e). Deployed on the cluster (cf586b9dc): the DOI digit-run fix and
the gloss / decade / chunk-fallback / micro-unit round (25abd28fd). Landed
on main, not yet deployed: the chunk-range / evidence-list / table-header
round (6c1326c9a), the parenthetical / `kind:` break / thousands round
(f4f0e82d0), the semicolon / glued-K round (81aa9d32e) and this commit's
round. Five prod dry runs (Reto, via the pgbouncer tunnel): 1,087 → 910 →
876 → 829 → 795 flagged of ~3,090 candidates (26%), 0 errors; by cite kind
pa 515 · me 294 · pc 223 · paper-key 207 · paper 65 · websearch 53. Every
40-row sample was read against the sources (each new round's rows checked
against the paper text by a read-only prod query). The fifth sample's six
new rows: four genuine (me39255 "1 ps" lee26b, me39410 "50%" brabson25,
me39504 "1.7 eV" martirez26, me39553 "30%" garridotorres17 — none in the
paper), one spelling (me39603 "1.78 Å": razzaq25 writes `$1.78±0.11$ Å`,
fixed for chunk-text evidence, document-level stays with gr478127), one
pinpoint seven chunks off (me39478 "1 kHz" at wo2021081641a1~313, cited
~306). `--apply` adds the tag; `--apply --clear-stale` (this commit) lifts
the system-set tag where the gate no longer fires, so a tag a later gate
round would not raise is not permanent.
False-positive classes fixed so far: (a) gloss parenthetical pinned to the
nearer following cite; (b) "2010s" as seconds; (c) chunk pinpoint a few
chunks off; (d) micro units the `numerics` column cannot see; (e) the
exponent of `10^7 cm/s`; (f) patent reference numerals `216A-N`; (g) a
`~a..b` range collapsed to a document cite; (h) table cells with the unit
in the header; (i) a parenthetical 50 chars after its cite pinned to the
next cite (me35525 — now any parenthetical glosses the nearest cite before
its opener, same sentence); (j) "… simulations. patent:x envisions" not a
sentence break because the next word is lowercase (me36301); (k) evidence
spelled "2,000 and 6,000 p.p.m." (me35789); (l) "… at 9nm; paper:y shows"
pinned 9 nm to the cite after the semicolon (me38505 — a semicolon now ends
the clause); (m) "130K low-quality molecules" read as kelvin (me38802,
me38803 — a glued `K` is a temperature only after a cue such as "at",
"below", "T ="). Genuine flags confirmed against the sources: me34865 "0.3
eV" hwang25 (the paper says 0.14 eV); me38567 "1.2 nm" bandhybridized25
(the paper says 1 nm); me38731 "18.5 kJ" sauer19 (the paper's values are
13.5, 16.5 and 21.0 kJ/mol); me34846
"100 GHz" zhu12a; me35569 "0.65 V" lamagni20 (the paper's potentials are
0.97 and 1.19 V — the number is hermawan23's); me35605 "80 °C"
naghshnejad25 (no °C in the text); me35024 / me35027 (review findings
quoting the mis-sourced numbers they report); slug collisions `anon24d`
(me23148) and `zhang09-bilayer-gap` (me34846, resolves to a DNA paper) —
gr477964. Residual classes, not fixed: a number derived next to its source
("75% … the missing 25%", "250–350 °C … ~100–150 °C earlier"); a scale
restated in an analogy sentence that cites the analogue (me35192 "1-10 nm"
against evans24, me36499 "±10%" against benoist25); a range's zero endpoint
("0–0.3 eV"); a websearch cited by its quoted query (`websearch:'…'`, not a
cite form, me35601); table-header units and list heads in document-level
`numerics` (me25065 "0.15 nm"; needs `extract_numerics` at ingest);
me38924 "10 Å" kocer24~7..10 is unverified (the chunks carry no Å value the
tokenizer sees — possibly a PDF spelling of Å, possibly a real miss); a
pinpoint many chunks off (me39478, ±1 and the document's `numerics` both
miss it). The ingest-side `numerics` gaps are gr478127.
Remaining, in order: (1) Reto re-runs the dry run on the fixed tree; (2) if
the sample reads as real plus the residuals above, `--apply` the backfill
in warn mode; the `reject` flip waits until the residual classes are rare
in a fresh sample. Delete this file in that commit.

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
   `2010s` is a decade, not seconds. A parenthetical glosses what precedes
   it: a number inside one belongs to the nearest citation before the
   opening bracket in the same sentence (`paper:x (… 60 kPa …) and
   paper:y` pins 60 kPa to x), unless a citation inside the brackets is
   nearer; otherwise it goes to the nearest citation in the sentence. A
   sentence ends before a lowercase `kind:` cite too (`… simulations.
   patent:x envisions`), and a semicolon ends a clause (`… at 9nm;
   paper:y shows`). A glued `K` (`130K molecules`) is a count unless a
   temperature cue precedes it (`at 77K`, `T = 4K`, `below 20K`). A
   pinpoint the prose writes after a cite — `paper:x (chunks ~97–98: …)`,
   `paper:x (chunk ~15, …)`, `paper:x (~151, ~157)` — scopes the evidence
   like `paper:x~97..98` would.
   The exponent of `10^7 cm/s` and a patent reference numeral `216A-N` are
   tokenizer artefacts, not claims.
4. **Evidence.** For a chunk-level cite: that chunk's text and its
   neighbours ±1. For a ref-level cite: `SELECT unnest(numerics)` over
   the ref's chunks (cheap for a 400-page textbook) plus the ref title;
   for websearch / perplexity / web / memory targets, the body chunks'
   text. Both sides go through `taproot/reword._canon_grounding`;
   grounding decided by `taproot/reword._is_grounded` (decimal-prefix
   tolerance, integers exact). Import those two helpers; do not copy them.
   A unit-bearing claim is grounded only by the same `(number, unit)` pair
   in the evidence — never by a bare digit run (DOIs, years and ref ids
   make every small integer "present" in any body). A chunk cite that
   misses its chunk ±1 is checked against the whole document (a pinpoint a
   few chunks off is a reading aid, not a mis-sourced number). Micro units
   (`µm`, `µA`, `µM`, `µg`, `µs`) never flag against paper/patent evidence:
   `numerics` cannot tokenise the `$\mu$ m` PDF text writes, so the claim
   is unverifiable, which is not ungrounded. A `~a..b` range cite is
   checked against every chunk in it (capped at 40). Evidence text is read
   generously: list members share the trailing unit ("0.44 and 0.26 eV"),
   markdown table cells take the unit their header names (`[Å]`), and
   "2,000 p.p.m." is read as "2000 ppm" alongside the raw spelling, and
   PDF math `$1.78±0.11$ Å` grounds both 1.78 Å and 0.11 Å.
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
  to the nearest; a gloss parenthetical pins to its opener unless an inner
  cite is nearer; `2010s` never fires; a chunk cite falls back to the
  document; micro units are unverifiable against numerics-only evidence;
  `10^7 cm/s` and `216A-N` are not claims; evidence lists and table headers
  ground their members; `~a..b` parses to the whole range; a parenthetical
  50 chars after its cite still glosses it; a lowercase `kind:` cite starts
  a sentence; thousands commas and `p.p.m.` in evidence; a semicolon ends
  the clause; `130K molecules` is a count, `at 77K` a temperature; a prose
  pinpoint after a cite becomes its chunk range; `$1.78±0.11$ Å` grounds
  both numbers.
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

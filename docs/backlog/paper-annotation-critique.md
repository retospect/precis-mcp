---
status: draft
prio: normal
---

# Paper annotation critique

Grouped 2026-09-26 from 2 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## paper review notes

_Grouped 2026-09-26; was `paper-review-notes`, status draft, prio normal._

Design session 2026-09-20 (Reto + agent), purrfect-floating-mango worktree.

### Motivation / why

A paper page has no place to put an opinion. What a reader thinks about a
paper — its limitations, whether a number is trustworthy, whether it
actually supports the thing everyone cites it for — lives in a session
transcript and dies there. The next agent to read the same passage starts
from zero.

The point of this build is not the web UI. It is that an annotation
reaches an agent **at the moment it reads the annotated passage** over the
MCP, the same way `_citer_sidecar` puts citation verdicts under a chunk.
The web tab is how a human writes one; the chunk sidecar is what makes it
worth writing.

### In scope

**Substrate.** A note is a `memory` ref — no new kind. `memory` is already
"capture notes, decisions, ideas, questions" (`handlers/memory.py`), its
prose lives in an embedded, keyworded `memory_body` chunk, `edit(id=N,
text=…)` rewrites in place, `delete(id=N)` soft-deletes. Search, dedup and
the numeric-ref CRUD shape come free.

**Two relations, because there are two kinds of annotation.** New
closed-vocab pairs in `data/skills/precis-relations.md`:

| `rel=` | Inverse | Use for |
|---|---|---|
| `annotates` | `annotated-by` | A **note** — an observation, idea, or pointer about the paper or passage. Additive. Makes no claim about whether the paper is sound. |
| `critiques` | `critiqued-by` | A **critique** — a challenge to the paper's reliability: a stated limitation, an overreaching claim, a citation that doesn't support what it's cited for. Bears on whether we should lean on this paper. |

The distinction is carried on the *edge*, not as a tag on the memory,
which matches how the vocabulary already puts stance on the relation
(`supports` / `disputes` / `raises-concern-about`) and makes the read path
split for free — the store read is already keyed on relation, so
"critiques on this block" is the same query with a different argument, not
a post-filter.

**Not** `raises-concern-about`: that pair means the formal publishing
Expression of Concern and sits next to the retraction machinery. A reader
thinking a sample is small must not light up the same signal as a journal
issuing an EoC.

Either relation targets `pa<id>` for a paper-level annotation or `pc<id>`
for a passage-level one. The chunk ord on the link is what lets the read
path bucket per block cheaply; a tag-filtered `related-to` cannot do that.

**Author handle.** Notes carry a short display handle: `reto`, `matthias`,
or `autoreviewer`. The `web_users.abbrev` column already exists for exactly
this and is documented as unread ("a future per-edit attribution UI renders
and links; nothing reads it in this cut", `users.py:91`) — this build is
its first reader. **Not** `refs.set_by`: `actors` is a four-row seeded
*channel* vocabulary (`agent`/`user`/`system`/`chase`) FK'd from refs,
chunks, links and tags; one row per person conflates who-typed-it with
which-pipe-it-came-through. `set_by` keeps meaning `user` vs `agent`.

**MCP read path (the load-bearing half).**
- Chunk-anchored annotations render as a capped sidecar under each block in
  `handlers/paper.py::_render_chunks`, in the slot `render_citer_sidecar`
  already occupies. Same shape: capped entries, best-first,
  expand-on-request. Each entry leads with its author handle.
- **Critiques and notes render as separate sections, critiques first**, and
  under independent caps. A chunk with one critique and nine notes must not
  push the critique off the bottom of a shared cap — that is the single
  failure this split exists to prevent. Notes are quiet; a critique is the
  thing an agent must not miss on a passage it is about to quote.
- Paper-level notes render **once**, in `_render_overview` / the `get`
  header — never repeated per block, or a 20-block read returns 20 copies
  of one opinion.
- One link query per *request* over the whole `(lo, hi)` range, bucketed by
  ord — not one per block. A paper with no notes costs one indexed miss.
- `PaperHandler.search_hits` is a separate path: a chunk hit carries a bare
  marker, counted by type ("2 notes, 1 critique"), not the bodies. The text
  arrives on the chunk read.
- Notes render for **every** agent, always attributed — not only for the
  note's author. **Decided.**

**Web.**
- Default the paper reader's tab to Meta: `routes/papers.py:507`, flip the
  `initial_tab` fallback from `Navigate` to `Meta`. `?tab=` still overrides;
  `pres` already hard-defaults Meta.
- A `Review` tab in `templates/_reader/reader.html.j2` — a server-rendered
  htmx panel lazy-loaded on `setTab()`, exactly like the Sources/Cited
  panels, gated per-kind the way `doc.show_refs_tabs` gates those.
- `POST` / `PATCH` / `DELETE` on the paper for note create/edit/delete.
  Precedent for the shape: `POST /se/{slug}/note`
  (`routes/blocktree_view.py:1219`).
- A count on the `/preview/paper/{id}` hover card (the popover behind every
  linkified `[pa123]`) — counts only, split by type, click-through to the
  tab. A paper carrying critiques should read differently at a glance from
  one carrying only notes.

### Explicitly NOT in scope

- **A new `note` kind.** `memory` is the note kind; a parallel kind buys
  nothing and cuts against the vocab-compaction direction.
- **The autoreviewer.** Separate item,
  `docs/backlog/paper-annotation-critique.md`. This build's job is the
  substrate and the read path it writes into.
- **An MCP write path for notes.** Agents read notes here; writing is the
  autoreviewer item's problem.
- **Inlining note text wherever a paper is cited.** The hover card gets a
  count. Pushing opinion text into draft citation views would put
  unreviewed annotation next to the paper's own claims — the boundary
  `cite-findings-only` and the draft-faithfulness policy already defend.
- **Dream consolidation over notes.** Deliberately undecided — gripe 372794
  holds the argument. Do not wire notes into `supersede` in this cut.
- **Threading / replies.** A flat, author-attributed accumulation. Rebuttal
  is another note.

### Acceptance criteria

- `/papers/havu2012functionalization` opens on Meta.
- A note written in the Review tab by a logged-in user appears in that tab
  with that user's `abbrev`, is editable and deletable there, and survives
  a reload.
- A note linked to `pc<id>` appears under that block — and only that block
  — when any agent reads the chunk over the MCP, carrying its author
  handle.
- A chunk carrying one critique and enough notes to exceed the note cap
  still shows the critique, in its own section, above the notes.
- A paper-level note appears once in the paper overview and in no chunk
  sidecar.
- Reading a 20-block range on a paper with no notes issues one additional
  query, not twenty.
- A chunk hit in `search_hits` shows a note count when notes exist and
  nothing when they don't.
- The paper's existing backlinks panel still lists the notes (it groups
  incoming edges by `(kind, relation)` already — `annotates` shows for
  free); no regression there.

### Target + blast radius

`handlers/paper.py` (`_render_chunks`, `_render_overview`, `search_hits`),
a new `handlers/_note_sidecar.py` modelled on `_citer_sidecar.py`,
`data/skills/precis-relations.md` (+ whatever validates the closed rel
list), a links-store read keyed on `(dst_ref_id, relation, dst_ord)`,
`precis_web/routes/papers.py`, `precis_web/templates/_reader/reader.html.j2`,
`precis_web/routes/preview.py`.

Read-path change on the single hottest MCP surface — the per-request query
budget in the acceptance criteria is the thing to watch post-deploy.

### Open questions / decisions log

- **Decided 2026-09-20:** `annotates`/`annotated-by` as a real rel, not a
  tag on `related-to`.
- **Decided 2026-09-20 (Reto):** note and critique are distinct, carried as
  two relations, rendered as two sections under independent caps.
- **Decided 2026-09-20:** notes render for every agent, always attributed.
- **Decided 2026-09-20:** agents read but do not write notes in this cut.
- **Decided 2026-09-20:** dream consolidation deferred → gripe 372794.
- **Open:** does the Meta default apply to the whole reader family (cfp,
  datasheet) or papers only?
- **Decided 2026-09-24 (Reto): slice boundary = hand-anchor now, merge C
  into A.** One slice, not two: Meta default + Review tab CRUD + paper-level
  notes in the overview + backlinks, TOGETHER WITH the chunk-anchor read path
  (`_render_chunks` sidecar, `search_hits` markers). Rationale: the earlier
  proposal put the chunk sidecar in a later slice, but all three real
  critiques captured so far are paper-level, so a sidecar shipped alone has
  nothing to render — it would land as dead code. Anchoring the existing
  critiques is a hand step, not a feature, and it is what gives the sidecar
  something to show on day one.
  ⚠ Anchoring data is thin and is itself a finding — see gripe 372863: of
  Matthias's three critiques on `microkinetic26` (me372797 particle size,
  me372798 subsurface H, me372799 no experimental validation), only the
  particle-size one has a defensible anchor (`pc2350636`); subsurface H
  appears nowhere in the ingested text; and the validation critique is in
  apparent tension with `pc2350636`, which DOES compare to literature
  experimental data. Cause: `pa167977` is a one-page Elsevier preview
  (`pdf_pages = [0,1)`). Re-check after the markup backfill runs.
- **Open:** should an open critique on a paper warn at *evidence-attach*
  time — when that paper's chunk is about to ground a `finding` hub? That is
  the highest-value consumer of the distinction and the reason the
  autoreviewer exists, but it reaches into the finding-mint path, which this
  item otherwise does not touch. Probably its own item.
- **Open:** does a critique carry a resolution state (open / addressed /
  withdrawn)? A limitation that the authors themselves concede is never
  "resolved"; a citation-correctness complaint that turns out to be wrong
  should stop shouting. Leaning: no state in this cut, a rebuttal is another
  annotation — but that means a wrong critique shouts forever.
- **Open:** what does the Review tab render for a note anchored to a chunk
  — inline at the block in the Navigate/Raw list, or listed in the tab with
  a jump link? (The reader already exposes `gotoPage(n)`.)

## autoreviewer — machine critique of papers we are about to lean on

_Grouped 2026-09-26; was `autoreviewer-paper-critique`, status draft, prio normal, blocked-by paper-review-notes._

### Motivation / why

A paper that grounds a `finding` gets leaned on hard: its passages become
evidence, its numbers get quoted into drafts, and its citations get
inherited by our prose. Today nothing systematically asks "is this paper
actually load-bearing?" before that happens. A human reviewer would ask
about stated limitations, sample size, whether the cited support says what
the citing sentence claims it says. We do that ad hoc, per paper, in
whatever session happens to notice.

The paper-review-notes build (see `paper-review-notes.md`) gives the
substrate: a note is a `memory` ref linked to `pa<id>` or a specific
`pc<id>`, surfaced to the agent inline when it reads that chunk. An
autoreviewer is a worker that writes those same notes from the machine
side — so a critique reaches the agent at exactly the moment it reads the
passage, not in a report nobody opens.

### In scope

- A worker pass that, for a selected paper, produces chunk-anchored notes
  in two families:
  - **limitations** — what the paper itself concedes (sample size, scope,
    assumptions, conditions not tested), anchored to the passage that
    concedes it.
  - **citation correctness** — where the paper's own claim outruns the
    support it cites. Precedent for the mechanics is
    `workers/inbound_chase.py` (it already resolves chunk-scoped `cites`
    edges with a verdict); this is the same shape pointed inward.
- Everything this worker writes is a **critique** (`rel='critiques'`), never
  a plain note — both families above bear on whether the paper is safe to
  lean on. The autoreviewer has no business filing "interesting figure".
- Critiques carry an author handle distinguishing them from human ones
  (`autoreviewer`, not a `web_users` abbrev), so the reader can weight them
  differently and a human can rebut one alongside.
- A trigger policy: which papers get reviewed. The obvious one is "papers
  cited by a live finding hub" — the set where being wrong is expensive.

### Explicitly NOT in scope

- Reviewing every paper in the corpus. This is a targeted pass over papers
  we depend on, not a corpus sweep — the cost is per-paper LLM work.
- Any auto-retraction, auto-dispute, or evidence demotion. The autoreviewer
  writes notes; it never mutates a finding's evidence or flips a verdict.
  A human reads the note and decides.
- Replacing the human review tab. Both write into the same note stream.
- Scoring papers with a number. A critique is prose anchored to a passage;
  a quality score invites ranking on something we cannot calibrate.

### Acceptance criteria

- Running the pass on a paper with known stated limitations produces notes
  anchored to the conceding passages, and reading those chunks over the MCP
  surfaces them inline.
- Critiques are attributable to `autoreviewer` and visually/structurally
  distinct from human-authored ones in both the web review tab and the MCP
  sidecar.
- A paper with no notes costs nothing on the read path (no sidecar, no
  extra query beyond the one the notes surfacing already does).
- The pass is budgeted and idempotent — re-running does not duplicate notes
  for the same passage.

### Target + blast radius

New worker under `src/precis/workers/`. Reads chunks + `cites` links;
writes `memory` refs + `critiques` links. Read-path rendering is already
owned by the paper-review-notes build (`handlers/paper.py::_render_chunks`
sidecar) — this item adds a producer, not a second renderer.

### Open questions / decisions log

- Trigger: on finding-hub mint, on demand, or a scheduled pass over the
  cited-by-a-hub set?
- Does citation-correctness checking require fetching the cited paper (an
  acquire), and what happens when it is not in the corpus?
- One note per issue, or one note per chunk bundling several?
- Should a limitation note that a human marks "wrong" feed back into the
  prompt for the next pass, or is that a later distillation item?

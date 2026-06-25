---
id: precis-draft-help
title: precis — the editable document kind
summary: author a living document as chunks — create, read (outline/verbatim), edit text, reorder/reparent, soft-delete; markdown-ish prose with [dc…] references (any handle) and [§paper~n] citations
applies-to: get/search/put/edit/delete (kind='draft')
status: active
---

# precis-draft-help — author a living document

A `draft` is an **editable, chunk-native document** — the living
source of a project's write-up. Postgres is canonical; it exports to
LaTeX/PDF/Word. Unlike a `paper` (frozen), a draft's chunks are mutable
in structure (reorder/reparent) and in text. **One draft per project**;
a snapshot/backup is a *freeze* (see below).

Everything goes through five verbs — **no new verbs**: `put` (create /
add a chunk), `edit` (change text **or** move), `get` (outline /
verbatim), `delete` (soft-retire), `search` (lexical / semantic over
prose). A draft is **not** taggable or linkable as a whole (`tag`/`link`
on `kind='draft'` raise `Unsupported`) — cross-references are markdown
refs embedded in prose, and the per-chunk autolinker materialises a
`related-to` backlink for each (see *References in prose*).

## Search a draft (lexical / semantic)

```python
search(kind='draft', q='direct air capture')                  # across ALL drafts
search(kind='draft', q='direct air capture', scope='test01')  # one draft
search(kind='draft', q='amine sites', scope='dc8')        # subtree under a heading
search(kind='draft', q='capture', mode='lexical')             # verbatim / keyword
search(kind='draft', q='capture', mode='semantic')            # by meaning (default: hybrid)
search(kind='draft', q='methods', headings_only=True)         # jump to a section heading
```

`mode=` is the same axis as everywhere else: `lexical` (exact / keyword),
`semantic` (meaning), default `hybrid` (both, fused). `scope=` narrows to
one draft (slug) or one section (a `dc<id>` → that chunk's subtree); omit
it to search every draft. `search(id='dc<id>', q='…')` is accepted too —
the handle already names the kind and the chunk is the scope. Each hit
shows its `draft:<slug>` and `dc<id>`; read one with `get(id='dc<id>')`.

## Find a project's draft

A draft carries **no `project:` tag** — that tag lives on the project
*todo*, and the draft is bound to it 1:1 by a `draft-of` link. So:

```python
get(kind='draft')                         # list ALL drafts (no project filter yet)
get(kind='todo', id='<project>', view='links')   # → follow the draft-of link to the slug
```

To go project → draft, resolve the project todo and follow its
`draft-of` link. (The planner prompt also tells an editor agent which
draft it is in, so this is rarely needed mid-edit.)

## Addressing — universal handles, never numbers

Each chunk has a stable **handle**: the computed universal handle
`dc<chunk_id>` (e.g. `dc41`) — `dc` = draft chunk, then its id. In verbs
just pass it: `id='dc41'` (handles are globally unique — no draft name
needed); the draft *record* is addressed by its name/slug or its `dr<id>`
handle. You **never** type or compute a handle for a *new* chunk — `put`
returns it. Reading windows use the relative grammar (`dc41-2..3`,
`dc41+1`, `dc41^`). Positional `~N` ordinals are **not** offered for
drafts (they rot on insert); use handles.

## Start a new draft

A draft is **born with a title heading** (so it is never empty), bound
1:1 to its project todo by a `draft-of` link. The brief lives on the
project's `meta.workspace.brief`; the draft carries `path`/`format`.

```python
# 1 — create the draft (returns the draft + its title heading dc1)
put(kind='draft', id='nanotrans', project='<project-todo-id>',
    title='Nanoscale Transistors',
    meta={'workspace': {'path': 'projects/nanotrans', 'format': 'tex'}})

# 2 — add a section heading after the title
put(kind='draft', id='nanotrans', chunk_kind='heading',
    text='Introduction', at={'after': 'dc1'})       # → returns dc12

# 3 — a paragraph under it
put(kind='draft', id='nanotrans', chunk_kind='paragraph',
    text='Nanoscale transistors …', at={'into': 'dc12', 'last': True})
```

`at` places the new chunk (all parts optional): `{'first'|'last': True}`,
`{'into': 'dc<id>'}`, `{'before'|'after': 'dc<id>'}`.

## Add prose — one paragraph per put

Write **one paragraph per `put`**. A longer `put` is split at block
boundaries (blank lines; lists/code/tables stay whole) and returns one
handle per chunk:

```python
put(kind='draft', id='nanotrans', chunk_kind='paragraph',
    text='First para.\n\nSecond para.', at={'after': 'dc12'})
# → returns [dc13, dc14]
```

**Every block carries prose — write the sentences, not just the
scaffold.** A paragraph that is only a citation, a formula, a list of
references, or a bare claim with no explaining text is incomplete: state
the point in running prose, *then* support it. If a block is genuinely
just a figure / equation / table, give it the matching `chunk_kind` and a
one-line caption — don't leave a "paragraph" that is structure without
saying anything. (Why it happens: it's easy to drop the evidence and
move on; the prose that ties it to the argument is the actual writing.)

**Style: plain prose, no emphasis markup.** Write in plain declarative
sentences, one idea each. **Do not use bold or italics for emphasis.**
`_italic_` and a single `*word*` are not rendered and leave literal `_`
and `*` markers in the text; `**bold**` does render but reads as shouting
in a research write-up. Let sentence structure carry the weight.
**No em-dashes** (the `—` character): split the thought into separate
sentences, or use a colon, comma, or parentheses instead. (Headings
already stand out, so you do not need bold on top.)

## Figures & images

A **figure** is a chunk whose caption is the face (`text`) and whose
image bytes live in the database (a `chunk_blobs` row, 1:1 with the
chunk — never in `text`). Add one with `chunk_kind='figure'`, the
caption as `text`, the image **base64** in `image=`, and an `origin=`:

```python
# our own diagram / schematic
put(kind='draft', id='nanotrans', chunk_kind='figure',
    text='Fig 1. Device cross-section.', image='<base64>',
    origin='original', at={'after': 'dc12'})

# a plot we generated from data (ships a data supplement — see graphs)
put(kind='draft', id='nanotrans', chunk_kind='figure',
    text='Fig 2. I–V curves.', image='<base64>', origin='own_graph')

# reused from another paper — REQUIRES the publisher paper-trail
put(kind='draft', id='nanotrans', chunk_kind='figure',
    text='Fig 3 (after Smith 2019).', image='<base64>',
    origin='third_party',
    permission={'publisher': 'Springer Nature',
                'permission_id': 'SNCSC-2026-0451',
                'status': 'granted',            # requested|granted|denied
                'requested_at': '2026-06-10', 'granted_at': '2026-06-18',
                'scope': 'this manuscript, print + electronic',
                'required_credit': 'Reprinted by permission …',
                'source_paper': 'smith19'})     # cite-key of the source
```

`origin` ∈ `{original, own_graph, third_party}` records where the figure
came from and drives a **clearance gate**: a `third_party` figure is
cleared only with a **granted, unexpired** permission. The reader shows a
warning banner listing any uncleared figures (and an all-clear note when
every figure passes), and **export fails** on an uncleared figure — so an
unlicensed image can't ship. A `third_party` figure **must** carry a
`permission` paper-trail — that is the whole point: track *with whose
permission*, *which permission number*, *when requested/granted*, *when it
expires*. `mime=` is sniffed from the bytes when omitted.
Permission lives in `meta.figure.permission`; the reader shows an origin
chip + a ✓/✗ clearance badge, and serves the image at
`/drafts/blob/<handle>`. In the **web reader** a per-block **"＋ figure"**
control uploads an image file directly (multipart) — for a `third_party`
image it reveals the permission form inline — so a human can drop in a
figure without base64. The clearance badge under a rendered figure is
**editable**: hover for the paper-trail, click to edit it. Programmatic
edits use `edit(kind='draft', id='dc<id>', origin='third_party',
permission={…})` — caption and image bytes stay put.

> Graph regeneration (the plot's data + code as `figure_code` /
> `figure_data` chunks linked `derived-from`) and the export step that
> writes images out to `pics/` are later phases.

## Data / table chunks

A `chunk_kind='table'` chunk holds **structured data, not prose**. Pass the
canonical data as `table={header, rows}` — *not* `text=`. The markdown you
read back is **derived** from that data (regenerated on every write, never
hand-edited), so the numbers stay the single source of truth and stay
searchable / numerics-indexable.

```python
put(kind='draft', id='nanotrans', chunk_kind='table',
    table={'header': ['element', 'gap_eV'],
           'rows': [['Si', 1.12], ['Ge', 0.67]]},
    caption='Measured band gaps',          # the legend (optional)
    regen={'source': 'dft', 'cmd': 'vasp relax'},  # how the data was made (optional, inert)
    at={'last': True})
```

* **`caption=`** is the table's legend — it rides in the derived text so the
  table is findable by it.
* **`regen=`** records provenance / how to rebuild the data (a sim, a command,
  an ingest pointer). It is **inert metadata** — recorded, never executed.
* **Editing:** change the data, not the rendered text. `text=` is *rejected*
  on a table chunk.

  ```python
  edit(kind='draft', id='dc42', table={'header': [...], 'rows': [...]})  # re-derives markdown
  edit(kind='draft', id='dc42', caption='New legend')   # caption only; data kept
  edit(kind='draft', id='dc42', regen={'source': 'manual'})  # provenance only
  ```
  (`dc<chunk_id>` is the chunk's address — `put` returns it; legacy `¶<handle>`
  still resolves on input.)

## Graph figures (computed from data)

A **graph** is a `figure` (the umbrella `chunk_kind`) whose image is *computed
from data*, not uploaded — `origin='own_graph'`. Instead of `image=`, give it
**`render=`** (the Python that draws it) and **`plots=[dc<id>]`** (the data/table
chunks it reads). The caption is `text=`, like any figure.

```python
put(kind='draft', id='nanotrans', chunk_kind='figure',
    text='Fig 2. Band gap vs lattice constant.',
    plots=['dc42'],                       # the data/table chunk(s) it renders
    render=('import matplotlib.pyplot as plt\n'
            't = data["tables"][0]\n'      # plotted chunks arrive as data["tables"]
            'plt.scatter([r[0] for r in t["rows"]], [r[1] for r in t["rows"]])'),
    at={'last': True})
```

- The render code runs **sandboxed, out-of-band** (never at `put` time): it
  receives `data = {'tables': [...]}` (your `plots` chunks, in order) and `out`
  (the PNG path); an unsaved matplotlib figure is auto-saved. The image is
  **deferred** — a placeholder until the render lands, then it refreshes
  automatically whenever the plotted data changes (the `plots` edge is the one
  reactive recompute, ADR 0035).
- An *image* figure (uploaded `image=`, `origin∈{original,third_party}`) and a
  *graph* (computed, `own_graph`) are the **same `figure` kind** — they differ
  only in where the pixels come from. Clearance, caption, blob serving, export
  all apply identically.

## Read the document

```python
get(kind='draft', id='nanotrans')          # outline: handle | §-path | gist
get(id='dc12')                           # one chunk, verbatim source
get(id='dc12-5..3')                      # that chunk + 5 before, 3 after
```

Navigate the **outline** first (cheap — one line per chunk), then pull
**verbatim** only for the region you act on. `dc<id>-2..3` is a
reading window (B before, A after, in reading order).

The outline ends with a **`## Work in progress`** block when todos are
working on this draft and are stuck or in flight — walked
draft → project → todo subtree. A `⚠ blocked` row carries a
`child-failed:<job>` bubble (a child job failed and parked the parent
out of the rotation); `⚙ in flight` is a live/queued job. Inspect with
`get(kind='todo', id=<id>)`; unblock a stuck todo by retrying, splitting,
or dropping it (`tag` off the `child-failed:` bubble + `STATUS:done`).
This is how a failed enrichment job *registers on the draft* instead of
silently stalling.

## Change a chunk's text

```python
edit(id='dc12', text='Nanoscale transistors, defined as …')
```

In-place: the handle (and every reference to it) survives; embeddings /
keywords / gist re-derive automatically.

## Reorder / move (structure, not a new verb)

```python
edit(id='dc16', move={'before': 'dc15'})                  # reorder among siblings
edit(id='dc17', move={'parent': 'dc20', 'after': 'dc18'}) # move into another section
edit(id='dc19', move={'into': 'dc20', 'last': True}) # to a section's end
```

Send the *intent* with handles; the system computes the ordering and
records it. No text changes → nothing re-embeds. Moving a heading
carries its whole subtree.

## Soft-delete (retire) — `delete`, reversible

```python
delete(id='dc12')                       # retire a chunk (un-delete restores)
delete(id='dc20', mode='promote')         # remove heading, keep contents (lift to parent)
delete(id='dc20', mode='cascade')         # delete heading AND its contents
```

A **heading with children requires a `mode`** — `promote` (keep
contents) or `cascade` (delete the section) — there is no default for
that destructive choice. Retired chunks drop out of the document but
their history (and any anchor to them) survives. You **cannot delete
the last live chunk** — a draft is never empty.

## References in prose — one link form

Prose is **markdown**. To reference anything, write `[<handle>]` — a
handle is a ref to *something*, and the system resolves it. That single
rule covers every cross-reference: a chunk in this draft (`[dc41]`), a
memory or finding (`[me5]`), a paper chunk (`[pc10]`). Use
`[text](<handle>)` when you want display words. The only non-handle form
is a **paper citation**, which is keyed on the cite_key so it can build a
bibliography:

| write | means | renders |
|---|---|---|
| `[<handle>]` | reference to whatever the handle names | a link (chunk → §/number; record → link) |
| `[the prior result](<handle>)` | reference with display text | hyperlinked text |
| `[§<cite_key>~<n>]` | **paper citation** | `[n]` + bibliography |
| `[DuckDuckGo](https://…)` | web link | hyperlink |

Cite the **exact** paper chunk that holds the detail (`[§miller89~4]`),
not the whole paper.

**Always cite a paper by `[§<cite_key>~<n>]` — the cite_key, never a
numeric id.** `[§singlemolecule13~2]` is right. The `§cite_key` form is
the portable one: it exports to a real `\cite{singlemolecule13}` and
reads as a citation, whereas a numeric id is opaque and produces no
bibliography entry. If a tool result hands you a paper as a number,
**translate it to its `cite_key`** before you write it (the cite_key is
the bare slug in `get(kind='paper', id=…)` / search output).

**Citation rigor (be strict).** A citation must **directly and
substantively support the specific claim** — you must be able to quote
the sentence(s) in the cited chunk that establish it (capture them as
the `source_quote` / `\citequote`). If you can't find a passage that
supports the claim, the cite is **too weak** — either:

- **soften the claim** to match the evidence ("suggests", "is
  consistent with", "reports") rather than asserting it, or
- **find a better source** (prefer the primary source for an empirical
  claim).

Never cite topically-related-but-non-supporting work, and **never cite
a source for a stronger claim than it actually makes** (citation
inflation). Match assertion strength to evidence strength: a single
study → tentative; replicated findings / a review / a meta-analysis →
strong. The reader's cite popover shows the cited chunk verbatim, so a
mismatch between claim and passage is visible — make them agree.

## Cite a paper we don't have yet — request it, don't fake it

The right source for a claim is often **not in the corpus yet**. That is
not a dead end, and it is **not** a reason to silently soften the claim:
soften only when the *evidence* is genuinely weaker, never because the
library merely lacks the paper. Every move below exists to **end with a
real, ingested paper chunk you can quote** — discovery tools find the
source, the corpus is the only thing you cite. Work cheapest /
highest-precision first:

1. **Re-check the corpus.** A semantic + lexical `search(kind='paper',
   q=…)` — we may already hold it under another slug/cite_key. Cheapest
   possible win.
2. **Mine the bibliographies of papers we already hold.** The primary
   source is almost always in the reference list of a review or
   related-work paper that *is* in the corpus. Walk it with Semantic
   Scholar — this hands you a real DOI, no guessing:

   ```python
   get(kind='semanticscholar', id='refs:<held-paper-doi>')   # papers it cites
   get(kind='semanticscholar', id='cites:<held-paper-doi>')  # papers citing it
   ```
3. **Find the canonical source by topic — as a pointer-finder, never the
   citation.** When no held paper points the way:

   ```python
   get(kind='semanticscholar', id='<title or topic>')   # structured hits → DOIs
   get(kind='perplexity-research', q='<question>')       # fills the gap, names the work
   ```

   Use S2 search first (it returns a structured DOI you can act on);
   Perplexity/websearch are the fallback. **Convert the answer into a
   resolvable id, then ingest it** — never cite Perplexity or a web page
   as the source of a scientific claim.
4. **Request it + park the citing work behind the ingest.** Once you have
   a resolvable id:

   ```python
   # a — request the paper (stub-only put; idempotent, DOI/arXiv preferred)
   put(kind='paper', doi='10.1038/nature10352')   # → fetch_oa grabs an OA PDF, watcher ingests, embedder indexes
   # (or arxiv='2401.00001' / identifier='s2:<id>'; title-only parks with no auto-fetch)

   # b — park a leaf that waits until that paper is ingested + embedded
   wait = put(kind='todo',
              text='[auto] wait for 10.1038/nature10352 ingested+indexed',
              meta={'auto_check': {'type': 'paper_ingested',
                                   'doi': '10.1038/nature10352',
                                   'timeout_at': '<ISO-8601, e.g. +7d>'}})

   # c — block the citing change-request on the wait so it leaves the
   #     doable rotation until the paper lands
   link(kind='todo', id='<your citing todo>', target=f'todo:{wait.id}',
        rel='blocked-by')
   ```

   The wait is a plain **todo leaf** (not a job): the `auto_check` worker
   polls it ~every minute and flips it to `STATUS:done` once the paper is
   ingested + embedded, re-entering your citing todo. `timeout_at`
   surfaces a stalled fetch for triage instead of waiting forever.
5. **No resolvable id, only a fuzzy claim?** Mint
   `put(kind='finding', text='<claim>', …)` and let `finding_chase`
   resolve it (Unpaywall / arXiv / S2 / EPO), then cite on a re-tick.
   This is the fallback — prefer a stub when you have an id, since it's
   deterministic.
6. **Only now consider softening.** If steps 2–3 turn up no source that
   actually supports the claim, *then* soften to match the evidence (or
   drop it).

In the meantime **do not** invent a `\cite{}` key, write `paper:slug`
for a paper that isn't held, or leave a bare `[citation pending]` with
nothing chasing it — a placeholder nobody is fetching never becomes a
citation. The stub/finding *is* the acquisition; the `[citation
pending]` (if you mark the spot) then has something behind it. See
`precis-stubs-help` (the acquisition backlog), `precis-auto-tasks-help`
(the wait-on-ingest pattern in full), and `precis-paper-help` (S2 nav +
held-paper citing).

**Abbreviations — write the short form; define it once via a term call.**
Use the abbreviation itself in prose (`TTA`, `PEI`, `FET`). **Do not spell
it out inline** as `Term To Abbrev (TTA)`: the reader shows the definition
on hover wherever the short form appears (including plural forms like
`FETs`), so the expansion in the sentence is redundant clutter. After any
`put`/`edit`, the response **hints any undefined acronyms you just wrote**,
with copy-ready calls. For each, either:

- **define it** — `put(kind='draft', id='<slug>', chunk_kind='term',
  text='Kil Solvent Joule Warbler', meta={'short': 'KSJW'})` (filed
  under an auto-created **Glossary** heading). This term call **is** what
  "define an abbreviation" means here — not an inline parenthetical; or
- **mark it not-an-abbreviation** (a chemical formula, a model name, …)
  — `edit(kind='draft', id='<slug>', not_abbrev=['CO2'])` — to silence
  the hint.

Once defined or silenced, a token stops being hinted. Reference a term with
`[PEI](<dc-term-handle>)`; explicit
terms win over auto-detected ones. **Thoughts** (memory / think / finding) are
referenceable but **not citeable** — they get a `[<handle>]` link only,
never a bibliography entry. Math is `$…$` / `$$…$$` (LaTeX, rendered by
KaTeX on the web).

**Don't write `[finding #<name>]`.** A finding is addressed by its base32
`pub_id` by its `fi<id>` handle (`[fi<id>]`), **not** by a
made-up `#slug`. A `[finding #amine-uptake]` /
`[citation pending — finding #…]` marker resolves to **nothing** — it
never autolinks, never exports, and on a verbatim read is flagged as an
**⚠ unresolved finding reference**. If you mean to cite a finding,
reference its real handle; if it doesn't exist yet, `put(kind='finding',
…)` it first (and remember: a finding is a `[<handle>]` link, not a `[§…]`
citation). Don't leave dangling `#name` placeholders in the prose.

**Formatting.** Prose is plain text with a small markup subset:
`` `code` `` renders inline code, `$…$` / `$$…$$` is math (KaTeX), and
`<sub>`/`<sup>` render for chemistry and units (`NH<sub>2</sub>`,
`g<sup>-1</sup>`). **Do not use emphasis markup.** `_italic_` and a single
`*word*` are not rendered at all and leave literal `_`/`*` in the text;
`**bold**` does render but reads as shouting, so skip it. Inline
citations and cross-refs render as a compact marker in the reader,
so handles do not clutter the sentence: write `[§miller89~4]` and it shows
as a small superscript. A chunk cross-ref must use the target chunk's
**`dc<id>` handle** (e.g. `[dc41]`), shown in the outline — never a
numeric id like `[45650]`, which resolves to nothing.

**Every** reference you write (a `[§paper~n]` citation or a `[dc<id>]`
link to a chunk/thought) auto-materialises a
`related-to` backlink, so the draft is
discoverable from the cited paper/thought's side; remove a reference and
its link drops on the next edit. Intra-draft `[dc<id>]` cross-refs are
document-internal (TOC / `\ref`), not graph edges.

## Writing well — structure + common mistakes

A research write-up is *flowing prose*, not a slide deck. When you write
or revise a block:

**Structure**

- **One paragraph, one idea — topic sentence first.** Lead with the
  claim; the rest of the paragraph develops it. Don't bury the point or
  fuse two ideas into one paragraph.
- **Claim → evidence → citation, in that order.** Each claim earns its
  evidence, then its `[§…]` cite. Don't stack unsupported assertions.
- **Given → new flow.** Open a sentence with familiar information, end
  with the new. Open each section with a sentence that says what it
  covers (signpost).

**Diction**

- **Consistent terminology** — one term per concept. No elegant
  variation on key terms (a synonym reads as a *different* thing).
- **Quantify** — a number + unit beats "significant / several / many".
- **Concise, active** — cut "it is important to note that", "in order
  to" → "to", "due to the fact that" → "because"; prefer active voice.
- **Tense** — past for what was done/found, present for established
  facts.

**Avoid (LLM tells)**

- Slide-deck/listy prose and over-bolding instead of paragraphs.
- Filler openings ("In recent years, X has attracted significant
  attention…").
- Mismatched calibration — over-hedging in one place, over-claiming
  ("proves", "clearly", "novel", "first") in another.
- Restating the brief, or repeating a point across blocks.

## Steer the draft — brief + change requests (don't hand-edit prose)

You usually don't rewrite prose directly; you **steer**:

```python
edit(id='nanotrans', meta={'workspace': {'brief': '…updated brief…'}})
put(kind='todo', parent_id='<project>', text='tighten this paragraph',
    meta={'anchor': 'dc12'}, ...)        # a change request, anchored
link(src='dc12', rel='derived-from', dst='memory:7x2')  # provenance
```

A change-request `todo` anchored to a handle flows through the normal
todo tree → dispatch → jobs; the executor decides whether to do it in
one job or fan out per section.

**If you (the executor) can't complete a request, ask clearly.** When
you yield an `ask-user:`, write a real question a human can act on, and
**reference chunks by their `dc<id>`** — never a numeric "chunk 0"
(drafts have no numeric chunk addresses; the reader can't find it). Bad:
`ask-user:see-chunk-0`. Good: `ask-user: '"remove this para" is anchored
at dc5 (the intro); did you mean dc5 or the sibling dc12?'`.
The ask surfaces on the draft block as a 🔔, linking to your run.

## Export (LaTeX) — `precis draft export`

A draft renders to a **compilable LaTeX project** with one command:

```
precis draft export <slug> [--out DIR]   # → main.tex + refs.bib + preamble.tex
precis draft export <slug> --pdf          # …and run latexmk to produce main.pdf
latexmk -pdf main.tex                     # biber + makeglossaries run for you
```

In the web reader, the **PDF** link (header) compiles on demand and
serves the result, cached by the draft's version token — so it only
recompiles after an edit. Hosts without a TeX toolchain get a friendly
"latexmk not installed" message instead of a build.

The export is a one-way resolution pass; the output is **disposable**
(re-export from the draft, never hand-edit the `.tex`). Everything
resolves automatically: each block gets a `\label{chunk:<handle>}` and a
`[dc<id>]` cross-ref becomes `\cref{chunk:h}`; `[§slug~n]` / `paper:slug~n`
citations become `\cite{slug}` with a `refs.bib` generated from the cited
papers (DOI/arXiv included when known); every defined abbreviation
becomes a `\newacronym` and each occurrence a `\gls{…}` (first use full,
later uses short), with the page-number "where it occurs" list in the
glossary. Authoring `[<handle>]` links and bare thought mentions render to
nothing (provenance only). This is why **citing the exact chunk** and
**defining your abbreviations** pays off — the exporter does the rest.

## Export — PDF (job) and Word/.docx

A draft renders to a real document. Two paths:

- **PDF** — `export_draft` → LaTeX → `latexmk`. This is **deterministic
  but slow**, so it runs as a **job**. Start one and watch its logs on the
  project's task page:

  ```python
  put(kind='job', job_type='draft_export', parent_id=<project-todo-id>,
      params={'draft': '<slug>'})
  ```

  The job streams `job_event` progress and lands the PDF path in its
  `job_summary` / `meta.pdf`. (Web: the **export PDF** button on the draft
  reader does exactly this.)
- **Word/.docx** — toolchain-free (python-docx), so it's **synchronous** —
  the web reader's **export .docx** link downloads it immediately. Citations
  resolve through the same paper lookup the PDF uses (identical references),
  with render-time acronym first-use expansion + an auto acronyms list.

Both are **disposable** — re-export from the draft; never hand-edit the
output. Citations must resolve (`[§slug~n]` → a paper in the corpus) or the
export marks a stub + warns.

## Freeze / snapshot (release + backup)

A *freeze* copies the draft's current chunks into an immutable
`paper`-like ref (versioned, searchable, citable), linked `snapshot-of`
the draft. The draft keeps evolving. (Operational verb TBD.)

## See also

```python
get(kind='skill', id='precis-citation-help')   # citation kind + verifier workflow
get(kind='skill', id='precis-paper-help')       # read, cite, search held papers
get(kind='skill', id='precis-stubs-help')       # request a paper we don't have (acquisition backlog)
get(kind='skill', id='precis-finding-help')     # flag a claim / chase an un-ingested DOI
get(kind='skill', id='precis-auto-tasks-help')  # wait-on-ingest (paper_ingested) leaf pattern
```

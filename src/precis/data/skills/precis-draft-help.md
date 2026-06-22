---
id: precis-draft-help
title: precis — the editable document kind
summary: author a living document as chunks — create, read (outline/verbatim), edit text, reorder/reparent, soft-delete; markdown-ish prose with ¶/§/[[ ]] references
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
search(kind='draft', q='amine sites', scope='¶jUcyv8')        # subtree under a heading
search(kind='draft', q='capture', mode='lexical')             # verbatim / keyword
search(kind='draft', q='capture', mode='semantic')            # by meaning (default: hybrid)
search(kind='draft', q='methods', headings_only=True)         # jump to a section heading
```

`mode=` is the same axis as everywhere else: `lexical` (exact / keyword),
`semantic` (meaning), default `hybrid` (both, fused). `scope=` narrows to
one draft (slug) or one section (a `¶handle` → that chunk's subtree); omit
it to search every draft. `search(id='¶handle', q='…')` is accepted too —
the `¶` already names the kind and the chunk is the scope. Each hit shows
its `draft:<slug>` and `¶handle`; read one with `get(id='¶<handle>')`.

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

## Addressing — opaque handles, never numbers

Each chunk has a minted, opaque **handle** (e.g. `5BL5xQ`). The
inline/prose sigil is `¶`, so a chunk is `¶5BL5xQ`; in verbs use
`id='¶5BL5xQ'` (handles are globally unique — no draft name needed).
You **never** type or compute a handle for a *new* chunk — `put`
returns it. Numeric `~N` ordinals are **not** offered for drafts (they
rot on insert); use handles.

## Start a new draft

A draft is **born with a title heading** (so it is never empty), bound
1:1 to its project todo by a `draft-of` link. The brief lives on the
project's `meta.workspace.brief`; the draft carries `path`/`format`.

```python
# 1 — create the draft (returns the draft + its title heading ¶t0)
put(kind='draft', id='nanotrans', project='<project-todo-id>',
    title='Nanoscale Transistors',
    meta={'workspace': {'path': 'projects/nanotrans', 'format': 'tex'}})

# 2 — add a section heading after the title
put(kind='draft', id='nanotrans', chunk_kind='heading',
    text='Introduction', at={'after': '¶t0'})       # → returns ¶k7m2aQ

# 3 — a paragraph under it
put(kind='draft', id='nanotrans', chunk_kind='paragraph',
    text='Nanoscale transistors …', at={'into': '¶k7m2aQ', 'last': True})
```

`at` places the new chunk (all parts optional): `{'first'|'last': True}`,
`{'into': '¶<heading>'}`, `{'before'|'after': '¶<handle>'}`.

## Add prose — one paragraph per put

Write **one paragraph per `put`**. A longer `put` is split at block
boundaries (blank lines; lists/code/tables stay whole) and returns one
handle per chunk:

```python
put(kind='draft', id='nanotrans', chunk_kind='paragraph',
    text='First para.\n\nSecond para.', at={'after': '¶k7m2aQ'})
# → returns [¶aa1, ¶aa2]
```

**Every block carries prose — write the sentences, not just the
scaffold.** A paragraph that is only a citation, a formula, a list of
references, or a bare claim with no explaining text is incomplete: state
the point in running prose, *then* support it. If a block is genuinely
just a figure / equation / table, give it the matching `chunk_kind` and a
one-line caption — don't leave a "paragraph" that is structure without
saying anything. (Why it happens: it's easy to drop the evidence and
move on; the prose that ties it to the argument is the actual writing.)

**Style: prose, lightly marked.** Write in plain declarative prose.
**Do not bold for emphasis** — heavy `**bold**` reads as shouting and
clutters the page. Reach for *italics* only **occasionally**, for a
genuinely emphasised word or a term on first mention; most paragraphs
need no emphasis at all. Let sentence structure carry the weight, not
markup. (Headings already stand out — you don't need bold on top.)

## Read the document

```python
get(kind='draft', id='nanotrans')          # outline: handle | §-path | gist
get(id='¶k7m2aQ')                           # one chunk, verbatim source
get(id='¶k7m2aQ-5+3')                       # that chunk + 5 before, 3 after
```

Navigate the **outline** first (cheap — one line per chunk), then pull
**verbatim** only for the region you act on. `¶<handle>-B+A` is a
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
edit(id='¶k7m2aQ', text='Nanoscale transistors, defined as …')
```

In-place: the handle (and every reference to it) survives; embeddings /
keywords / gist re-derive automatically.

## Reorder / move (structure, not a new verb)

```python
edit(id='¶B', move={'before': '¶A'})                  # reorder among siblings
edit(id='¶3', move={'parent': '¶secB', 'after': '¶7'}) # move into another section
edit(id='¶x', move={'into': '¶heading', 'last': True}) # to a section's end
```

Send the *intent* with handles; the system computes the ordering and
records it. No text changes → nothing re-embeds. Moving a heading
carries its whole subtree.

## Soft-delete (retire) — `delete`, reversible

```python
delete(id='¶k7m2aQ')                       # retire a chunk (un-delete restores)
delete(id='¶secB', mode='promote')         # remove heading, keep contents (lift to parent)
delete(id='¶secB', mode='cascade')         # delete heading AND its contents
```

A **heading with children requires a `mode`** — `promote` (keep
contents) or `cascade` (delete the section) — there is no default for
that destructive choice. Retired chunks drop out of the document but
their history (and any anchor to them) survives. You **cannot delete
the last live chunk** — a draft is never empty.

## References in prose — markdown links

Prose is **markdown**; references are markdown links the renderer
resolves per target:

| write | means | renders |
|---|---|---|
| `[DuckDuckGo](https://…)` | web link | hyperlink |
| `[¶<handle>]` | cross-ref to this draft | computed §/number |
| `[§<paper>~<n>]` | **citation** to a paper chunk | `[n]` + bibliography |
| `[the prior result](¶<handle>)` | cross-ref with display text | hyperlinked text |
| `[surface words](¶<term-handle>)` | glossary term | first-use / abbreviation |
| `[[memory:<id>]]` | **authoring** link (any thought) | nothing (provenance only) |

Cite the **exact** paper chunk that holds the detail (`[§miller89~4]`),
not the whole paper.

**Always cite by `[§<cite_key>~<n>]` — the cite_key, never a numeric
ref id.** `[§singlemolecule13~2]` is right; `paper:liu24~3` and
`paper:1837~3` are **wrong** in a draft. Three forms technically
resolve — `[§liu24~3]` (the canonical sigil), `paper:liu24~3` (the bare
`kind:ref` mention, sugar), and `paper:<numeric-ref-id>~3` — but only the
`§cite_key` form is portable: it exports to a real `\cite{liu24}` and
reads as a citation, whereas a numeric ref id is opaque, unstable across
re-ingest, and produces no bibliography entry. If a tool result hands you
a paper as a number or a `paper:` mention, **translate it to its
`cite_key`** before you write it (the cite_key is the bare slug in
`get(kind='paper', id=…)` / search output; `[§<cite_key>~<chunk>]`).
Write **one** form per citation — never both `[§slug~n]` and
`paper:slug~n` for the same reference, or the reader shows a redundant
chip.

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

**Abbreviations — use them freely; we'll ask you to define what we don't
recognise.** Write with abbreviations naturally. After any `put`/`edit`,
the response **hints any undefined acronyms in what you just wrote**,
with copy-ready calls. For each, either:

- **define it** — `put(kind='draft', id='<slug>', chunk_kind='term',
  text='Kil Solvent Joule Warbler', meta={'short': 'KSJW'})` (filed
  under an auto-created **Glossary** heading); or
- **mark it not-an-abbreviation** (a chemical formula, a model name, …)
  — `edit(kind='draft', id='<slug>', not_abbrev=['CO2'])` — to silence
  the hint.

An inline `Full Form (ABBR)` first-use also counts as a definition. Once
defined or silenced, a token stops being hinted. Reference a term with
`[PEI](¶<term-handle>)`; explicit
terms win over auto-detected ones. **Thoughts** (memory / think / finding) are
referenceable but **not citeable** — they get a `[[…]]` link only,
never a bibliography entry. Math is `$…$` / `$$…$$` (LaTeX, rendered by
KaTeX on the web).

**Don't write `[finding #<name>]`.** A finding is addressed by its base32
`pub_id` (`finding:<pub_id>` or `[[finding:<pub_id>]]`), **not** by a
made-up `#slug`. A `[finding #amine-uptake]` /
`[citation pending — finding #…]` marker resolves to **nothing** — it
never autolinks, never exports, and on a verbatim read is flagged as an
**⚠ unresolved finding reference**. If you mean to cite a finding,
reference its real handle; if it doesn't exist yet, `put(kind='finding',
…)` it first (and remember: a finding is a `[[…]]` link, not a `[§…]`
citation). Don't leave dangling `#name` placeholders in the prose.

**Formatting.** Prose is markdown: `**bold**` renders bold and
`` `code` `` renders inline code. Reach for emphasis **sparingly** — a
research write-up reads as prose, not a slide deck; bold the occasional
key quantity or term, not whole sentences. Math is `$…$` / `$$…$$`
(KaTeX). Inline citations/cross-refs render as a compact `§`/`¶` marker
in the reader, so don't worry about handles cluttering the sentence —
write `[§miller89~4]` and it shows as a small superscript.

Bare `kind:ref` mentions (`paper:miller89~4`, `memory:6184`) are
recognised too — the bracket forms are the *superset* over the same
grammar notes use. **Every** reference you write auto-materialises a
`related-to` backlink (the same shared autolinker), so the draft is
discoverable from the cited paper/thought's side; remove a reference and
its link drops on the next edit. Intra-draft `¶` cross-refs are
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
    meta={'anchor': '¶k7m2aQ'}, ...)        # a change request, anchored
link(src='¶k7m2aQ', rel='derived-from', dst='memory:7x2')  # provenance
```

A change-request `todo` anchored to a handle flows through the normal
todo tree → dispatch → jobs; the executor decides whether to do it in
one job or fan out per section.

**If you (the executor) can't complete a request, ask clearly.** When
you yield an `ask-user:`, write a real question a human can act on, and
**reference chunks by their `¶handle`** — never a numeric "chunk 0"
(drafts have no numeric chunk addresses; the reader can't find it). Bad:
`ask-user:see-chunk-0`. Good: `ask-user: '"remove this para" is anchored
at ¶MwJjhD (the intro); did you mean ¶MwJjhD or the sibling ¶k7m2aQ?'`.
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
`[¶h]` cross-ref becomes `\cref{chunk:h}`; `[§slug~n]` / `paper:slug~n`
citations become `\cite{slug}` with a `refs.bib` generated from the cited
papers (DOI/arXiv included when known); every defined abbreviation
becomes a `\newacronym` and each occurrence a `\gls{…}` (first use full,
later uses short), with the page-number "where it occurs" list in the
glossary. Authoring `[[…]]` links and bare thought mentions render to
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
the draft. The draft keeps evolving. (Operational verb TBD; see
ADR 0033.)

## See also

`precis-draft-prose`, `precis-draft-structure`, `precis-draft-citation`,
`precis-draft-glossary`, `precis-draft-math`, `precis-draft-export`.
Design: ADR 0033.

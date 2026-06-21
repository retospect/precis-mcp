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

## Read the document

```python
get(kind='draft', id='nanotrans')          # outline: handle | §-path | gist
get(id='¶k7m2aQ')                           # one chunk, verbatim source
get(id='¶k7m2aQ-5+3')                       # that chunk + 5 before, 3 after
```

Navigate the **outline** first (cheap — one line per chunk), then pull
**verbatim** only for the region you act on. `¶<handle>-B+A` is a
reading window (B before, A after, in reading order).

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
not the whole paper. **One syntax per citation** — `[§slug~n]` and
`paper:slug~n` are the *same* reference (`§` is sugar); write one, not
both, or the reader shows a redundant chip.

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
latexmk -pdf main.tex                     # biber + makeglossaries run for you
```

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

## Freeze / snapshot (release + backup)

A *freeze* copies the draft's current chunks into an immutable
`paper`-like ref (versioned, searchable, citable), linked `snapshot-of`
the draft. The draft keeps evolving. (Operational verb TBD; see
ADR 0033.)

## See also

`precis-draft-prose`, `precis-draft-structure`, `precis-draft-citation`,
`precis-draft-glossary`, `precis-draft-math`, `precis-draft-export`.
Design: ADR 0033.

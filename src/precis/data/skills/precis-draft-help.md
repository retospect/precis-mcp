---
id: precis-draft-help
family: drafting
title: precis — the editable document kind
summary: author a living document as chunks — create, read (outline/verbatim), edit text, reorder/reparent, soft-delete; markdown-ish prose with [dc…] links (any handle) and bare [pc…] paper-chunk citations
answers:
  - how do I create a new draft, or fork/scaffold one from an existing document?
  - how do I search inside a draft — lexical, semantic, or regex?
applies-to: get/search/put/edit/delete (kind='draft')
status: active
tags: [drafting]
kinds: [draft]
---

# precis-draft-help — author a living document

A `draft` is a project's **editable, chunk-native write-up**. Postgres is
canonical; export supports LaTeX/PDF/Word. Unlike frozen `paper`s, draft
chunks can be reordered, reparented and rewritten. **One draft per project.**

Five verbs, no new ones: `put` (create / add a chunk), `edit` (change text
**or** structure), `get` (outline / verbatim), `delete` (soft-retire),
`search` (lexical / semantic over prose). `tag`/`link` on `kind='draft'`
raise `Unsupported` — a draft is not taggable/linkable as a whole;
cross-references are markdown refs embedded in prose (how a handle routes:
`precis-draft-cite-help`); the per-chunk autolinker adds a backlink —
`cites` for a citable source (paper/patent/finding), `related-to`
otherwise. The edge is grounded on both ends: the source `dc<id>` (which
paragraph cites it) and the target. On export only a `cites` edge to a
source-grounded finding hub (or a paper/patent/datasheet it grounds on)
is a citation (Reto, 2026-10-07): links made while authoring — fisheye
hops to memory, concept or note nodes — are authoring aids and drop. A
claim that lives only in such a node needs a finding minted or found
before it can be cited.

The web reader URL `/smartdraft/<id>` (or `/smartdraft/<slug>`) is this
same draft: read it with `get(kind='draft', id='dr<id>')`, never
`webfetch`.

## Quick reference — verbs, views, edit params, grammar

See [[precis-draft-reference-help]].

## Search a draft — lexical, semantic, regex

```python
search(kind="draft", q="direct air capture")  # across ALL drafts
search(kind="draft", q="direct air capture", scope="test01")  # one draft
search(kind="draft", q="amine sites", scope="dc8")  # subtree under a heading
search(
    kind="draft", q="capture", mode="lexical"
)  # verbatim / keyword (default: hybrid)
search(kind="draft", q="methods", headings_only=True)  # jump to a section heading
```

`scope=` narrows to one draft (slug) or one section (`dc<id>` → that
chunk's subtree); omit it to search every draft. `search(id='dc<id>',
q='…')` is accepted too — the handle already names the scope. Each hit
shows `draft:<slug>` and `dc<id>`; read one with `get(id='dc<id>')`.

For regex search and scoped substitution examples, see
[[precis-draft-reference-help]].

## Create a draft — new, fork, or scaffold

A draft carries no `project:` tag — that lives on the project *todo*; the
draft is bound 1:1 by a `draft-of` link. `get(kind='draft', project=…)`
resolves the project todo and returns the bound draft's outline
(mutually exclusive with `id=`):

```python
get(kind="draft")  # list ALL drafts
get(kind="draft", project="<project-todo-id>")  # → that project's draft outline
```

Unbind or rebind the owner with `link(kind='draft', id='<slug>',
target='todo:N', rel='draft-of', mode='remove'|'add')`; add refuses while a
live owner exists. Readers skip a retired owner; `precis draft
audit-owners` counts them.

A draft is born with a title heading (never empty), bound 1:1 to its
project todo. The brief lives on the project's `meta.workspace.brief`;
the draft carries `path`/`format`.

```python
put(
    kind="draft",
    id="nanotrans",
    project="<project-todo-id>",
    title="Nanoscale Transistors",
    meta={"workspace": {"path": "projects/nanotrans", "format": "tex"}},
)  # 1 — creates the draft + its title heading dc1
put(
    kind="draft",
    id="nanotrans",
    chunk_kind="heading",
    text="Introduction",
    at={"after": "dc1"},
)  # 2 — a section heading → returns dc12
put(
    kind="draft",
    id="nanotrans",
    chunk_kind="paragraph",
    text="Nanoscale transistors …",
    at={"into": "dc12", "last": True},
)  # 3 — a paragraph under it

put(
    kind="draft",
    id="dc12",
    chunk_kind="item",
    text="a new item",
)  # id=<chunk> writes into its draft, placed at={"after": that chunk}
  # (an explicit at= still wins placement; the anchor then names the draft)

put(
    kind="draft",
    copy_of="nanotrans",
    project="Nanotrans review pass",
    id="nanotrans-r2",
)
# → fork: forked draft 'nanotrans-r2' bound draft-of a freshly minted project; source untouched

edit(kind="draft", id="nanotrans", scaffold="paper")
# → scaffold: Abstract, Introduction, Related Work, Methods, Results, Discussion, Conclusion
```

`put`'s declared params stop at `text`/`title`/`meta`/`args` —
`chunk_kind`, `at` and the other draft params ride `args=`:
`put(kind='draft', id='<slug>', text='…', args={'chunk_kind':
'paragraph', 'at': {'after': 'dc12'}})`. Top-level still lands, with a
deprecation note.

**Fork** deep-copies every chunk (live and retired), hierarchy, and every
link touching it, into a NEW draft; source is never touched. `project=`
is required — an existing project todo *or* a title string that mints
one; refuses if that project already owns a draft. `id=` seeds the new
slug (deduped `-2`/`-3`… if omitted, default `<src>-copy`). The copy
starts fully unreviewed (review history not carried over).

**Scaffold** appends a document class's standard section skeleton after
whatever is already there. Classes: `paper`, `patent`, `report`,
`review` (survey), `manufacturing`, `book` (Preface, Introduction,
Background, Chapter 1-3, Conclusion, Bibliography), `summary` (short
digest — Summary, Key Points, Details, References). Unknown class →
`BadInput` listing valid ones; `id` may be the slug or any
`dc<id>`/`¶handle` inside it. Never overwrites or reorders — only
appends; re-scaffolding an already-scaffolded draft adds a second copy,
so scaffold once, early.

Place a section at the FRONT with `args={'at': {'before':
'dc<first-section-heading>'}}`; an `after` anchor on the intro paragraph
makes the heading a sibling of that paragraph — at the document end when
it is the last root-level chunk.

## Length budgets & section styles (heading chunks)

```python
edit(id="dc<heading>", word_target={"min": 200, "max": 400})  # {} clears
edit(id="dc<heading>", style="<section-style skill>")
```

`word_target=` bounds are non-negative ints, `min <= max`, either bound
omittable; counts come from `view='wordcount'` (a section includes its
subsections) and the web reader badges off-target sections. `style=`
tells review personas and scaffolded genres what the section should look
like. Both are generic draft params, not proposal-specific.

Forms that cap a box in **characters** (grant portals: "350 characters")
use `char_target`, same shape, set through `args=`:
`edit(id="dc<heading>", args={"char_target": {"max": 350}})` (`{}`
clears). The count is the section's prose text with **every character
counted, spaces and punctuation included**; inline `[dc…]`/`[¶…]` refs
are stripped and markdown links count their visible text. `view='wordcount'`
then adds `chars` / `char_target` / `char_verdict` columns (over/under/ok)
and a `⚠ … off char target` line; the web smartdraft rollup lists the
same sections. Word and char targets are independent and can both be set.

## Document metadata — rename & byline

```python
edit(kind="draft", id="nanotrans", title="Nanoparticle transport in packed beds")
edit(
    kind="draft",
    id="nanotrans",
    authors=[
        {
            "name": "Doe, Jane",
            "affiliation": "Massachusetts Institute of Technology",
            "ror": "https://ror.org/042nb2s44",
            "orcid": "0000-0002-1825-0097",
        },
        {"name": "Roe, John", "affiliation": "Caltech"},  # affiliation/ror/orcid optional
    ],
)
```

`title=` writes `refs.title` and the title heading atomically, repairing
drift while preserving anchors. `id` is the slug or any `dc<id>`/`¶handle`
in it. Blank title → `BadInput`. Without a root heading, only the ref changes.

`edit(kind="draft", id=<slug>, args={"language": "en-GB"})` sets the draft
language (`""` clears): the Word export writes it to `w:lang`/`dc:language`;
unset exports en-US. Tags must be at most 255 characters.

`ror` is the institution's [ROR](https://ror.org) id — authors sharing
one collapse to a single numbered affiliation. Renders in the web reader
and both exports (PDF via `authblk`, .docx), org name linked to its ROR;
no affiliations → a plain name list. Web reader has the same
editor: an **authors ▾** dropdown, one author per line as `Name |
Affiliation | ROR`, posting to `/drafts/<slug>/authors`.

## Add prose — one paragraph per put

Write **one paragraph per `put`**. A longer `put` splits at block
boundaries (blank lines; code/tables stay whole, a bullet block becomes a
structured list — see below), returns one handle per chunk:

```python
put(
    kind="draft",
    id="nanotrans",
    chunk_kind="paragraph",
    text="First para.\n\nSecond para.",
    at={"after": "dc12"},
)  # → returns [dc13, dc14]
```

**Every block carries prose** — a paragraph that's only a citation, a
formula, a list, or a bare claim with no explaining text is incomplete:
state the point, then support it. A genuine figure/equation/table gets
the matching `chunk_kind` + a one-line caption, not a bare "paragraph."

**Plain prose, no emphasis markup** — `**bold**` and single-`*` italic
both render but read as shouting; `_italic_` does NOT render and leaves
literal `_` (collides with `$x_1$` math subscripts). No em-dashes (`—`)
or `--`. Split the sentence first. Use a comma or parentheses only for
a short gloss such as a unit or a three-word definition. Use a colon
to introduce a list or explanation.
Math (`$P_5$`), backtick code, URLs and `[handles]` are exempt. A
violation trips a `⚠ house style` hint on write, and `view='hygiene'`
counts them per rule with example `dc` handles. Two `*` forms stay
literal: an adsorbate star touching an element symbol (`*NO`, `H*`) and
the `\*` escape.

**Keep the sentence structure simple.** Put the subject, verb and object
first. Give each sentence one main fact. Keep clauses out of the space
between the subject and its verb. Give an aside its own sentence or move
it to the end. Rewrite "X, which Y, does Z." as "X does Z. It Y."

- **Before** — Carbon nanobuds, reported in 2007, are hybrid nanostructures
  formed by covalently or non-covalently bonding fullerenes to the sidewalls
  of carbon nanotubes or to a plane of graphene.
- **After** — Carbon nanobuds are hybrid nanostructures in which fullerenes
  bond to the sidewalls of carbon nanotubes or to graphene. They were
  reported in 2007.
- **Before** — Chemical reactivity, field emission (on films only) and the
  local electronic spectrum (two peaks on a single bud, no gap extracted)
  have measurements on nanobud material.
- **After** — Three properties have been measured on nanobud material:
  chemical reactivity, field emission and the local electronic spectrum.
  Field emission was measured on films only. The spectrum shows two peaks
  on a single bud, and no gap was extracted.

**No sentence opens with a lowercase identifier** — `autocatpath`,
`precis`, a lowercase glossary short ("autocatpath, the reaction
engine, …") reads as a typo at line start, and the ban on emphasis
means no italic rescue. Open with "The <identifier> …" instead, or set
the identifier in `backticks` — inline code is the documented form for
identifiers in prose and renders `\texttt` in the PDF, Consolas in
docx. Write/edit hint the opener and `view='hygiene'` flags it.

**Units & temperatures: literal sign, spaced off the value.** `63 °C` —
space, then degree sign `°` (U+00B0), then `C`. Range `63–65 °C`;
tolerance `±1 °C` (`±` = U+00B1, not `+/-`). Not a superscript, not `℃`,
not `63oC`/`63ºC`, not LaTeX (`^\circ`), not tight (`63°C`), not
`63° C`, not spelt out. SI separates a value from a unit symbol and `°C`
is one; the degree of an **angle** is not, so angles stay tight: `85°`.
Same rule governs claim sentences (`precis-notation-canon`), so prose and
claims cannot disagree. A malformed temperature trips a
`⚠ temperature/unit formatting` hint on write.

## Write a list — markdown bullets, converted on write

Write the list as ordinary markdown. A paragraph `put` whose text is
wholly a list lands as a `ulist`/`olist` container with one `item` chunk
per bullet; indentation nests. The response says so and names the
container.

```python
put(
    kind="draft",
    id="nanotrans",
    chunk_kind="paragraph",
    text="- NO side: N-O scission [fi348958]\n"
         "    - Bader charge shows 0.39 e transferred\n"
         "- NH3 side: Faradaic efficiency",
)
# → markdown bullets → structured list: dc91 [ulist] holding 3 items
```

Why it converts rather than staying bullet text: only the web reader
renders markdown bullets. The PDF and docx exports build lists from the
container/item shape — bullet text inside a paragraph reaches them as one
run-on line with literal hyphens. Structured, each item is also its own
addressable `dc` handle: citable, editable, reviewable.

A ulist needs at least two items (nested ones count), and *every* line
must be a bullet or a continuation of the one above it — a one-bullet
paragraph, or one that merely opens with a dash, stays literal text, no
container minted. Converted something you meant as prose?
`edit(kind='draft', id='dc<container>', list_kind='normal')` dissolves it
back to paragraphs.

The outline shows a list as one row (`dc91 [ulist] 3 items: NO side · …`);
`get(kind='draft', id='dc91')` renders its items in full. The qualified form
`id='<slug>~dc91'` (or `dr<ref>~dc91`) is equivalent; the selector also
takes a heading's exact title (`id='<slug>~Statement on AI Use'`) — it
resolves to that heading's `dc<N>`, and several headings sharing the title
refuse with their handles. A chunk outside that draft is NotFound.

## Add a figure, code block, or data table

See [[precis-draft-rich-content-help]]. A **data table** is
`chunk_kind='table'` with its grid as `table={header, rows}` — or a
markdown table in `text=` (`**caption**` line optional, cells typed
Excel-style):

```python
put(
    kind="draft",
    id="nanotrans",
    chunk_kind="table",
    table={"header": ["element", "gap_eV"], "rows": [["Si", 1.12]]},
    caption="Band gaps",
)
put(
    kind="draft",
    id="nanotrans",
    chunk_kind="table",
    text="| element | gap_eV |\n|---|---|\n| Si | 1.12 |",  # same table, markdown form
)
```

## Read the document — outline, verbatim, fisheye

```python
get(kind="draft", id="nanotrans")  # outline: handle | §-path | gist
get(kind="draft", id="nanotrans", q="amine")  # passages inside it (find-in-ref)
get(id="dc12")  # one chunk, verbatim source
get(id="nanotrans~Methods")  # ~<heading title> → that heading's dc chunk
get(id="dc12-5..3")  # reading-order window: 5 chunks before → 3 after,
                     # signed offsets from dc12 (not a sibling run)
get(id="dc12+1")  # next sibling under the same parent; dc12^ = enclosing heading
get(id="dc12", view="fisheye")  # verbatim center + reading-order neighbors
get(id="dc12", view="fisheye+1hop")  # fisheye + everything it points at
```

A range anchored on a heading stays inside that heading's subtree,
including nested sections; negative offsets do not add preceding context.
Ranges anchored on other chunk kinds cross headings and clamp at the
document ends. Both endpoints are signed offsets: `dc12+41..80` reads
offsets 41 through 80, while `dc12-41..80` starts 41 chunks before dc12.
Large ranges stop at a whole chunk, name the last chunk shown, and give
an exact `next:` range retaining the original anchor. Follow that handle
to read the remaining window without skipping nested sections. A single
chunk too large for the budget refuses with a retry instruction; no partial
chunk is shown. Plain list-container reads use the same whole-chunk limit
and continuation. Recovery notices and other footers also count toward the
budget; if a single chunk with its footer cannot fit, the read refuses.

A chunk read shows the addressed chunk alone — when it has children, the
footer counts them and names the `dc<N>-0..K` span that reads the whole
subtree inline (children are contiguous in reading order).

Reading order follows `pos`, not insertion time. Inspect placement with
`get(id="dc<id>", view="fisheye")`. Read the outline before prose.
For view rungs, reference rings and section eyes, see [[precis-fisheye-help]].

The outline’s **Work in progress** block lists blocked/in-flight todos
from the draft’s project. Inspect with `get(kind="todo", id="<id>")`.
Retry, split, or drop failed work; dropping removes the `child-failed:`
bubble and sets `STATUS:done`.

## Edit, review & retire a chunk

See [[precis-draft-review-help]]. Three traps:

- `edit(id='dc<id>', args={'move': {'into': 'dc…'}})` reparents the whole
  chunk — a list container moves with its items, a heading with its
  subtree. `delete(id='dc<id>', mode='cascade')` retires it with its
  children (`'promote'` lifts them).
- `edit(find=…, text='')` deletes a span, but a strict client can strip
  the empty `text=''` before it reaches the server; the reliable door is
  a whole-chunk `mode='replace'` rewrite minus the span.
- An aborted call or "connection closed" does not mean the write failed —
  it usually landed. `get(id='dc<id>')` and read back before retrying; a
  blind retry can double-apply.

## Cite and cross-reference in prose

See [[precis-draft-cite-help]], including `view='cites'`.

## Define an abbreviation — hover-resolve, no inline spellout

**Write the short form; define it once via a term call.** Use the
abbreviation in prose (`TTA`, `PEI`, `FET`) — do **not** spell it out
inline as `Term To Abbrev (TTA)`: the reader shows the definition on
hover wherever the short form appears (plurals like `FETs` too),
so an inline expansion is redundant clutter. A hand-written `Long form
(SHORT)` of a defined term is **flagged**: export expands the first
use; write the short alone. After any `put`/`edit`, the
response **hints any undefined acronyms you just wrote**, with
copy-ready calls:

```python
put(
    kind="draft",
    id="<slug>",
    chunk_kind="term",
    text="Kil Solvent Joule Warbler",
    meta={"short": "KSJW"},
)  # define it — filed under an auto-created Glossary heading
edit(
    kind="draft", id="<slug>", not_abbrev=["CO2"]
)  # OR: not-an-abbreviation — silences the hint
edit(
    kind="draft", id="<slug>", is_abbrev=["CO2"]
)  # inverse: un-silence a wrongly-ignored token
```

The term call **is** what "define an abbreviation" means here, not an
inline parenthetical. Want the label to stay the long form
(`short='stereolithography'`) but the acronym to also hover-resolve? Add
both: `meta={'short': 'stereolithography', 'abbrev': 'STL'}` — a
distinct resolvable surface, not a replacement for `short`. Defined or
silenced tokens stop being hinted; reference a term with
`[PEI](<dc-term-handle>)` (explicit terms win over auto-detected ones).

Batch terms with `put(kind='draft', id='<slug>', terms=[{'text': '…',
'meta': {'short': 'ABBR'}}])`. Strings work too. Without `at`, terms file
under their registry heading.
For mixed kinds, pass `chunks=[{'text': '…', 'chunk_kind': 'paragraph',
'meta': {}, 'at': {'last': True}}]`. Table text is a markdown grid.
View figures use caption text and `meta.view`.
Verb-level kind, metadata and placement provide defaults. Anchors must
resolve in this draft. Each batch takes up to 50 entries and rolls back
on indexed errors.

## Audit the draft — hygiene checks & the gap-finder

See [[precis-draft-review-help]] — audits, hygiene checks, the
gap-finder.

## Steer prose changes; correct unsupported claims directly

See [[precis-draft-review-help]] — steering prose edits, fixing
unsupported claims.

## Export the draft

See [[precis-draft-export-help]]. A **drifted cite** — a `[fi<id>]`
whose hub was reworded after the citing chunk was written — **blocks
export**; rewrite the passage to the hub's current sentence (a
whole-chunk `mode='replace'` re-pins even when the prose still stands).
Undefined-abbreviation warnings are advisory — define the token
(`chunk_kind='term'`) or silence it (`edit(not_abbrev=[…])`). A
`doc_type='poster'` draft is branded by a `kind='profile'` profile
(`meta.workspace.poster.profile`, else the owner's default) — see
[[precis-profile-help]].

## See also

- [[precis-citation-help]] — citation kind + verifier workflow.
- [[precis-paper-help]] — read, cite, search held papers.
- [[precis-stubs-help]] — request a paper we don't have (acquisition backlog).
- [[precis-finding-help]] — flag a claim / chase an un-ingested DOI.
- [[precis-fisheye-help]] — a chunk + its neighbourhood/reference ring.
- [[precis-auto-todo-help]] — wait-on-ingest (`paper_ingested`) leaf pattern.
- [[precis-taproot-help]] — cite a claim hub (living `[fi<id>]`).
- [[precis-taproot-mint-help]] — mint a claim hub.
- [[precis-taproot-backfill-help]] — backfill `[pc<id>]`/`[pa<id>]` cites to hub cites.
- [[precis-draft-reference-help]] — the verb/view/edit-param tables and move/table/authors grammar.
- [[precis-draft-review-help]] — edit/review/retire a chunk, hygiene + gap-finder, steering.
- [[precis-draft-cite-help]] — citations vs links in prose, never fabricate, request an unheld paper.
- [[precis-draft-rich-content-help]] — figures, images, code blocks, and data tables.
- [[precis-draft-export-help]] — LaTeX/PDF/Word/reMarkable export.
- [[precis-profile-help]] — owner-scoped branding profiles for poster drafts.
- [[precis-write-paper-help]] — prose craft: structure, diction, LLM tells to avoid.
- [[precis-ai-contributions-help]] — the "AI contributions" statement: per-model lines from provenance.

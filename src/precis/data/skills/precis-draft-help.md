---
id: precis-draft-help
title: precis — the editable document kind
summary: author a living document as chunks — create, read (outline/verbatim), edit text, reorder/reparent, soft-delete; markdown-ish prose with [dc…] links (any handle) and bare [pc…] paper-chunk citations
answers:
  - how do I create a new draft, or fork/scaffold one from an existing document?
  - how do I search inside a draft — lexical, semantic, or regex?
  - how do I cite a paper I don't have yet without faking the reference?
applies-to: get/search/put/edit/delete (kind='draft')
status: active
tags: [drafting]
kinds: [draft]
---

# precis-draft-help — author a living document

A `draft` is an **editable, chunk-native document** — the living source of
a project's write-up. Postgres is canonical; it exports to LaTeX/PDF/Word.
Unlike a `paper` (frozen), a draft's chunks are mutable in structure
(reorder/reparent) and in text. **One draft per project.**

Five verbs, no new ones: `put` (create / add a chunk), `edit` (change text
**or** structure), `get` (outline / verbatim), `delete` (soft-retire),
`search` (lexical / semantic over prose). `tag`/`link` on `kind='draft'`
raise `Unsupported` — a draft is not taggable/linkable as a whole;
cross-references are markdown refs embedded in prose (see *References in
prose*), and the per-chunk autolinker materialises a backlink for each —
`cites` for a citable source (paper/patent/finding), `related-to`
otherwise. The edge is grounded on both ends: the source `dc<id>` (which
paragraph cites it) and the target.

## Quick reference — verbs, views, edit params

**Addressing.** Every chunk has a stable handle `dc<id>` (e.g. `dc41`) —
globally unique, no draft name needed; the draft *record* is its slug or
`dr<id>`. Never guess/compute a handle — `put`/search/get return it.
Windows: `dc41-2..3` (2 before, 3 after), `dc41+1`, `dc41^` (parent). No
positional `~N` ordinals (they rot on insert).

**`get` — views**

| call | returns |
|---|---|
| `get(id='<slug>')` / `view='outline'` | handle \| §-path \| gist, whole doc |
| `get(id='dc<id>')` | one chunk, verbatim |
| `get(id='dc<id>-B..A')` | that chunk + B before, A after |
| `get(id='dc<id>', view='fisheye')` | verbatim center + graduated neighborhood |
| `get(id='dc<id>', view='fisheye+1hop')` | fisheye + cited/cross-ref/note ring |
| `get(id=<scope>, view='toc')` | heading skeleton (whole draft, or one heading's subtree) |
| `get(id=<scope>, view='hygiene')` | undefined-abbrev + unresolved-citation lists, full |
| `get(id=<scope>, view='backfill')` | uncited-but-relevant papers, gap-finder |
| `get(id=<scope>, view='wordcount')` | per-section word counts vs targets |
| `get(id='<slug>', view='links')` | the draft's link graph (cites/cross-refs/notes) |
| `get(kind='draft', project=<todo-id>)` | reverse lookup: that project's draft |

**`edit` — params** (`text=` rewrite is the default; one param family per
call)

| param | does |
|---|---|
| `text=` | whole-chunk rewrite |
| `find=` (+`text=`) | find-replace within the chunk (implies `mode='find-replace'`) |
| `dry_run=True`/`'full'` | preview a `text=`/`find=` edit, write nothing |
| `move=` | reorder/reparent — grammar below |
| `table=` / `cell=` / `find=`+`text=` / `sub=` | table-chunk cell edits — grammar below |
| `sub=` | regex substitute over a draft/section, dry-run by default, `apply=True` commits |
| `review=` | record a checker's sign-off (`'human'` or a worker name) |
| `authoring=` | `'on'`/`'off'` — let review personas author fixes inline |
| `authors=` | replace the byline — grammar below |
| `title=` | rename (both `refs.title` and the heading, atomically) |
| `scaffold=` | append a document class's section skeleton |
| `word_target=` | a heading's word budget `{'min':…,'max':…}`; `{}` clears |
| `style=` | stamp a heading with a section-style skill |
| `not_abbrev=` | silence the undefined-abbreviation hint for given tokens |
| `origin=` / `permission=` | a figure's provenance + clearance paper-trail |

Structural ops (`move`/`table`/`authors`) have no diff and reject
`dry_run`; `sub=` previews by default and commits on `apply=True`.

## Quick reference — move/table/authors grammar, put

**`move=` grammar**

| form | effect |
|---|---|
| `{'before'\|'after': 'dc<id>'}` | reorder among siblings |
| `{'parent': 'dc<id>', 'before'\|'after'\|'last': …}` | reparent |
| `{'into': 'dc<id>', 'last': True}` | append into a section |

```python
edit(id="dc16", move={"before": "dc15"})  # reorder among siblings
edit(id="dc17", move={"parent": "dc20", "after": "dc18"})  # move into another section
edit(id="dc19", move={"into": "dc20", "last": True})  # to a section's end
```

Moving a heading carries its whole subtree; no text changes, nothing
re-embeds.

**Table-chunk edit grammar** (`chunk_kind='table'`; plain `text=` is
rejected on a table chunk)

| call | effect |
|---|---|
| `table={'header':…, 'rows':…}` | whole grid, re-derives markdown |
| `cell='A1'` or `{'row':,'col':}` + `text=` | one cell (1-based; row 1 = header) |
| `find=` + `text=` | find-replace across string cells (literal) |
| `sub='s/a/b/'` | regex across cells, commits immediately, no dry-run |
| `caption=` / `regen=` | metadata only, data untouched |

**`authors=` grammar** — a list, each entry `{'name', 'affiliation'?,
'ror'?}` (or `{'family','given',…}`, or a bare name string). Replaces the
whole byline (not additive).

**`put`** creates: a new draft, a chunk (`at=` places it —
`{'first'\|'last': True}`, `{'into': 'dc<id>'}`, `{'before'\|'after':
'dc<id>'}`), or a fork — see *Create a draft*, below.

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

**Regex find + substitute** (vi `/pattern` and `:%s/a/b/`) — for a
**literal** pattern (markup, punctuation, a malformed citation), not
meaning: Python regex (`\w`, `\d`, groups, `|`) over chunk text. Audits
house style: stray `**bold**`, em-dashes, double spaces, a bare
`paper:123` cite.

```python
search(kind="draft", mode="regex", q=r"\*\*\w+\*\*", scope="nanotrans")  # find **bold**
search(kind="draft", mode="regex", q="TODO", scope="nanotrans", flags="i")  # case-fold
edit(
    kind="draft",
    id="nanotrans",
    sub={"find": r"\*\*(\w+)\*\*", "replace": r"\1"},
    apply=True,
)  # backreferences: strip bold
```

Find hits show `draft:<slug> dc<id> [kind]` and, per match,
`L<line>:<col>` with the span wrapped `»…«`. `flags='i'` case-folds,
`flags='s'` makes `.` cross newlines; `^`/`$` anchor per line; reads
table/figure text too (read-only). `replace` is a regex template
(`\1`/`\g<name>` resolve); every occurrence in each chunk is replaced —
`s/pat/repl/` string form works too, and `sub=` dry-runs (counts +
before→after sample) unless `apply=True`. Rewritten chunks go through
the normal edit path (re-embed/keywords/gist re-derive, prior text kept
in history); table/figure chunks are skipped in a slug/section-scoped
substitute — point `sub=` at the table's own `dc<id>` to edit its cells
instead.

**Scope** (find and substitute):

| `scope=`/`id=` | covers |
|---|---|
| a draft slug | the whole draft |
| a `dc<id>` heading | that section's subtree |
| a `dc<id>` leaf | just that chunk |
| omitted (find only) | every draft |

Substitute **requires** a scope — no corpus-wide rewrite.

## Create a draft — new, fork, or scaffold

A draft carries no `project:` tag — that lives on the project *todo*; the
draft is bound 1:1 by a `draft-of` link. `get(kind='draft', project=…)`
resolves the project todo and returns the bound draft's outline
(mutually exclusive with `id=`):

```python
get(kind="draft")  # list ALL drafts
get(kind="draft", project="<project-todo-id>")  # → that project's draft outline
```

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
    copy_of="nanotrans",
    project="Nanotrans review pass",
    id="nanotrans-r2",
)
# → fork: forked draft 'nanotrans-r2' bound draft-of a freshly minted project; source untouched

edit(kind="draft", id="nanotrans", scaffold="paper")
# → scaffold: Abstract, Introduction, Related Work, Methods, Results, Discussion, Conclusion
```

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
        },
        {"name": "Roe, John", "affiliation": "Caltech"},  # affiliation/ror optional
    ],
)
```

`title=` writes both `refs.title` (search hits, link chips) and the
title heading chunk, atomically — repairs an already-drifted state too;
the heading is edited in place (anchors stay live). `id` may be the slug
or any `dc<id>`/`¶handle` inside it. Blank title → `BadInput`. A draft
with no root heading (an import) renames the ref alone and says so.

`ror` is the institution's [ROR](https://ror.org) id — two authors
sharing a ROR collapse to one numbered affiliation. Renders in the web
reader and both exports (PDF via `authblk`, .docx), org name hyperlinked
to its ROR; no affiliations → a plain name list. Web reader has the same
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
— split the sentence, or use a colon/comma/parens.

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

Two bullets minimum, and *every* line must be a bullet or a continuation
of the one above it, so a paragraph that merely opens with a dash stays
prose. Converted something you meant as prose?
`edit(kind='draft', id='dc<container>', list_kind='normal')` dissolves it
back to paragraphs.

The outline shows a list as one row (`dc91 [ulist] 3 items: NO side · …`);
`get(kind='draft', id='dc91')` renders its items in full.

## Add a figure or a data table

See [[precis-draft-rich-content-help]].

## Read the document — outline, verbatim, fisheye

```python
get(kind="draft", id="nanotrans")  # outline: handle | §-path | gist
get(id="dc12")  # one chunk, verbatim source
get(id="dc12-5..3")  # that chunk + 5 before, 3 after
get(id="dc12", view="fisheye")  # verbatim center + reading-order neighbors
get(id="dc12", view="fisheye+1hop")  # fisheye + everything it points at
```

Navigate the outline first (cheap), then pull verbatim only for the
region you act on. `view=` rungs each strictly contain the previous:
`kwd` (ancestor path + bookmark) → `summary` (gloss) → `verbatim` (full
text) → `fisheye` (graduated span, ±5 full text/±10 gloss/±15 bookmark,
under the ancestor heading) → `fisheye+1hop` (+ the reference ring:
cited papers/patents/datasheets, cross-referenced `[dc…]`/`[¶…]` chunks,
linked notes; a cited `[fi<id>]` naming a live Taproot claim hub gets
its own **Claims** group — see `precis-fisheye-help`). Only wired for
`dc<id>`/`¶<base58>` on `kind='draft'` today, not `kind='plan'`.

The outline ends with a **`## Work in progress`** block when todos
working on this draft are stuck or in flight (walked draft → project →
todo subtree): `⚠ blocked` carries a `child-failed:<job>` bubble, `⚙ in
flight` is a live/queued job. Inspect with `get(kind='todo', id='<id>')`;
unblock by retrying, splitting, or dropping (`tag` off the
`child-failed:` bubble + `STATUS:done`) — how a failed enrichment job
registers on the draft instead of silently stalling.

## Edit, review & retire a chunk

```python
edit(id="dc12", text="Nanoscale transistors, defined as …")  # whole-chunk rewrite
edit(id="dc12", find="60°C", text="65°C")  # find= implies find-replace
edit(id="dc12", text="… big rewrite …", dry_run=True)  # PREVIEW the diff, write nothing
edit(
    kind="draft", id="dc12", review="human", verdict="needs-rework"
)  # record a sign-off
edit(
    kind="draft", id="nanotrans", authoring="on"
)  # let review personas author fixes inline
delete(id="dc12")  # retire a chunk (un-delete restores)
delete(id="dc20", mode="promote")  # remove heading, keep contents (lift to parent)
delete(id="dc20", mode="cascade")  # delete heading AND its contents
delete(id="nanotrans")  # ref-level id → soft-delete the WHOLE draft
```

`find=` is located **literally**; every occurrence is swapped for
`text=` (pass `text=''` to delete a span). If `find=` isn't present the
edit is **refused**, chunk untouched. `dry_run=True` gives a unified
diff; `dry_run='full'` shows the whole post-edit chunk. (For a regex
substitution across a whole section, use `edit(sub=…)` above.)

`review=` names the checker (`'human'` is the single human identity; an
automated checker like `'cites'`/`'flow'` records the same way from a
worker). `verdict=` free text, default `'approved'`. Upsert keyed on
`(chunk_id, checker)` — metadata only, no re-embed; a later text edit
makes the chunk "dirty" for that checker again. Web reader's ✓ gutter
button drives this; no un-review verb — re-review overwrites the prior
row. `authoring=` is a per-document flag, default off — on, the
`cites`/`structure` review personas edit the draft inline (mint a
grounded citation, extend/add a chunk stamped
`authored_by='review:<persona>'`) instead of only filing a change-request
todo, whenever they can ground the fix; `flow`/`adversarial` never
author regardless. Web reader toolbar carries the same switch.

A heading with children requires `delete(mode=…)` — `promote` (keep
contents) or `cascade` (delete the section) — no default for that
destructive choice. Retired chunks drop out of the document but their
history (and any anchor to them) survives; you cannot delete the last
live chunk — a draft is never empty.

`delete` reads its granularity off the id: a chunk address (`dc<id>` /
`¶<base58>`) retires that chunk, a **ref-level** id (the slug, or the
numeric ref id) soft-deletes the **whole document** — ref plus every
chunk, one transaction, recoverable. The owning project todo is left
intact: that deletes the document, not the project.

## References in prose — handles route by what they name

Prose is **markdown**. Reference anything by copying its `[<handle>]`
from search/get output (never guess); `[text](<handle>)` adds display
words. Two routes:

| write | route | means | renders / exports |
|---|---|---|---|
| `[fi<id>]` finding hub (grounded on `[pc<id>]` paper / `[pk<id>]` patent chunks) | **citation** | this hub's evidence supports the claim | `cites` edge + one bibliography entry per originator paper at export |
| `[dc<id>]` draft chunk, `[me<id>]` memory, any other kind | **link** | provenance / cross-ref | `related-to` backlink; never in the bibliography |
| `[text](<handle>)` / `[text](https://…)` | (either) / web | display text / web link | hyperlink |

Cite the **finding hub** (`[fi41]`), never the paper or chunk directly
— the hub is grounded on the exact chunks (`source_handle='pc234'`), and
several hubs backing one sentence sit side by side: `[fi41][fi92]`.
Export resolves each hub → its originator paper(s), renders `\cite{}` +
one bibliography entry per paper; you never type `\cite{}` yourself. A
bare `[pc<id>]`/`[pa<id>]` is a legacy cite — convert it (backfill
below, or by hand per `precis-citation-help`). A **link** (`[me<id>]`, cross-draft `[dc<id>]`) is never a
citation — provenance only, dropped on removal; intra-draft `[dc<id>]`
cross-refs stay document-internal (TOC/`\ref`), not a graph edge.

**Rigor.** Must **directly support the specific claim** — read the
hub's evidence first (`get(id='fi<id>', view='evidence')`), then the
grounding chunk (`get(id='pc<id>')`). Too weak? **Soften** ("suggests")
or **find a better source** (prefer the primary); never cite
topically-related-but-non-supporting work, or a stronger claim than the
source makes. Match strength to evidence: single study → tentative;
replicated/review/meta-analysis → strong (the cite popover shows the
cited chunk verbatim, so a mismatch is visible). A bare paper mention
(no chunk) only surfaces keyword labels to a later pass; missing the
right `pc<id>`? `get(kind='paper', id='<slug>~lo..hi', view='toc')`
re-clusters the range into finer groups — narrow and repeat.

**Backfill raw cites to a living hub cite** via a todo:
`put(kind='todo', text='taproot backfill <slug>',
meta={'executor': 'claude_inproc', 'job_type': 'taproot_backfill',
'params': {'scope': <slug-or-dc>}})` — converts `[pc<id>]`/`[pa<id>]`
cites to `[fi<id>]` claim-hub cites on the cluster worker; poll
`get(kind='job', id='jo<id>')`. See `precis-taproot-backfill-help`.

**Never fabricate a handle** — a `put`/`edit` that *introduces* a
handle-shaped `[…]` reference resolving to nothing (a numeric id like
`[45650]`, a typo'd `[dc…]`) is **refused** (`BadInput`, nothing
written): copy the handle from search/get output and retry. A
deliberate forward reference uses a `finding #<slug>` marker instead —
that lands, but is flagged **⚠ unresolved** on a verbatim read (never
autolinks, never exports). Mean a finding? Use its real `[fi<id>]`;
doesn't exist yet? `put(kind='finding', …)` it first.

**Formatting.** `` `code` ``, `$…$`/`$$…$$` math (KaTeX), `<sub>`/`<sup>`
for chemistry/units (`NH<sub>2</sub>`, `g<sup>-1</sup>`); no emphasis
markup (see *Add prose*, above). Citations/cross-refs render as a
compact superscript, so handles don't clutter the sentence. A chunk
cross-ref uses the target's `dc<id>` handle, never a numeric id like
`[45650]` (refused on write). Math must actually be math: a `$…$` span
with unbalanced `{ }`, or two money-dollars accidentally pairing across
prose (`$10-50 …, versus $200`), is demoted to escaped literal text by
both exporters and trips a `⚠ math that won't render` hint on write —
escape a literal dollar as `\$`.

## Define an abbreviation — hover-resolve, no inline spellout

**Write the short form; define it once via a term call.** Use the
abbreviation in prose (`TTA`, `PEI`, `FET`) — do **not** spell it out
inline as `Term To Abbrev (TTA)`: the reader shows the definition on
hover wherever the short form appears (including plurals like `FETs`),
so an inline expansion is redundant clutter. After any `put`/`edit`, the
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
)  # OR: mark not-an-abbreviation, silences the hint
```

The term call **is** what "define an abbreviation" means here, not an
inline parenthetical. Want the label to stay the long form
(`short='stereolithography'`) but the acronym to also hover-resolve? Add
both: `meta={'short': 'stereolithography', 'abbrev': 'STL'}` — a
distinct resolvable surface, not a replacement for `short`. Once defined
or silenced, a token stops being hinted; reference a term with
`[PEI](<dc-term-handle>)` (explicit terms win over auto-detected ones).

## Cite a paper we don't have yet — request it, don't fake it

1. **Re-check the corpus.** `search(kind='paper', q=…)` — may already be
   held under another slug/cite_key.
2. **Find the source, never cite the finder.** Mine bibliographies of
   held papers, or `get(kind="semanticscholar", id="refs:<doi>"|"cites:<doi>"|"<title or topic>")`;
   `get(kind="perplexity-research", q="<question>")` as fallback — convert
   whatever it names to a resolvable id and ingest that, never cite the
   web page itself.
3. **Request it + park the citing work behind the ingest.**

   ```python
   put(kind="paper", doi="10.1038/nature10352")  # idempotent request
   wait = put(
       kind="todo",
       text="[auto] wait for 10.1038/nature10352 ingested+indexed",
       meta={"auto_check": {"type": "paper_ingested", "doi": "10.1038/nature10352",
                             "timeout_at": "<ISO-8601, e.g. +7d>"}},
   )
   link(kind="todo", id="<your citing todo>", target=f"todo:{wait.id}", rel="blocked-by")
   ```

4. **No resolvable id, only a fuzzy claim?** `put(kind='finding',
   text='<claim>', …)`; `finding_chase` resolves it, then cite on a re-tick.
5. **Still nothing?** Soften the claim to match the evidence, or drop it.

Never invent a paper-chunk handle or write `paper:slug` for a paper not
held — cite the in-flight chase `[fi<id>]` until the paper lands and a
hub is grounded on it. See
`precis-stubs-help`, `precis-auto-todo-help`, `precis-paper-help`.

## Audit the draft — hygiene checks & the gap-finder

Things the runtime flags before export: an undefined abbreviation (see
*Define an abbreviation*, above), a citation that resolves to nothing
(see *References in prose*, above — cite a `[fi<id>]` hub grounded on the
exact chunk, never the paper), and a **drifted cite** — the hub was
reworded after this passage was written, so the prose paraphrases a
sentence that no longer exists. The cite still resolves (the old
`pub_id` is kept as an alias), which is exactly why it needs flagging:
nothing else makes it visible. The line quotes both statements — `was
"<old>", now "<new>"` — so the fix is one edit. **Rewriting the citing
chunk re-pins it**; a write elsewhere in the draft does not, and does not
clear the flag. A drifted cite **blocks export**; cites written before
version pinning existed are reported as unknown, never as drift. Neither needs a hand-maintained
bibliography footer — citation handles resolve to one entry per paper at
export. Skim the **outline** (`get(kind='draft', id=…)`) first — cheapest
place to catch both; its hygiene footer truncates each list to 8 entries.
For the full, un-elided lists, use `get(kind='draft', id=…,
view='hygiene')` — same two checks, no outline body, no truncation.

**Missed a source?** `get(kind='draft', id=<scope>, view='backfill')`
sweeps the corpus for relevant-but-**uncited** papers and assembles an
eyes workspace around the candidates — semantic + citation-graph recall,
deduped against everything the draft already cites (including the
supporting papers behind every cited `[fi<id>]` claim hub, so a hub's
own evidence never resurfaces as a false gap). `id=` is a `dc<id>`
section (full per-candidate detail) or a draft slug (a slimmer roll-up).
A topic-precision gate keeps candidates on-domain when the cited papers
carry `topic:` tags — a no-op when they don't. Text-driven — it programs
its own recall from the section's own keywords. For a query *you* phrase
("what does the corpus say about X that this draft hasn't already
grounded?"), use `search(q=…, uncited=<draft>)` instead — the same
already-cited closure, applied to a hand-typed search. See
`precis-search-help`.

**Some drafts are machine-owned and refuse your edits.** A draft linked
`dossier-of` (a quest's dossier, [[precis-quest-help]]) or `paper-of` is
written by the process that owns it; `put`/`edit`/`delete` on one raises
`Unsupported`. Read it freely — if such a draft looks wrong, the fix
belongs in the process that writes it, not in the document.

## Steer prose changes; correct unsupported claims directly

Prose craft (structure, diction, LLM tells to avoid) lives in
[[precis-write-paper-help]]. Here, steering:

**Steer for craft; correct directly for evidence.** A claim a held
source contradicts is a defect, not authorial intent — reword it to what
the source states (or delete an unsupported quantity; write both when two
held sources disagree), cite the `[fi<id>]` hub grounded on the passage
you read, and never substitute a number you did not read in a source.
Taste-level changes — voice, structure, framing — you steer instead:

```python
edit(id='nanotrans', meta={'workspace': {'brief': '…updated brief…'}})
put(kind='todo', parent_id='<project>', text='tighten this paragraph',
    meta={'anchor': 'dc12'}, ...)        # a change request, anchored
link(src='dc12', rel='derived-from', dst='memory:7x2')  # provenance
```

A change-request `todo` anchored to a handle flows through the normal
todo tree → dispatch → jobs; the executor decides one job vs fan-out per
section. **Can't complete a request? Ask clearly**, referencing chunks
by their `dc<id>` — never a numeric "chunk 0" (drafts have no numeric
addresses). Bad: `ask-user:see-chunk-0`. Good: `ask-user: '"remove this
para" is anchored at dc5 (the intro); did you mean dc5 or the sibling
dc12?'`. The ask surfaces on the draft block as a 🔔, linking to your run.

## Export the draft

See [[precis-draft-export-help]].

## See also

- [[precis-citation-help]] — citation kind + verifier workflow.
- [[precis-paper-help]] — read, cite, search held papers.
- [[precis-stubs-help]] — request a paper we don't have (acquisition backlog).
- [[precis-finding-help]] — flag a claim / chase an un-ingested DOI.
- [[precis-fisheye-help]] — `view='fisheye'`/`'fisheye+1hop'` — a chunk + its neighborhood/reference ring.
- [[precis-auto-todo-help]] — wait-on-ingest (`paper_ingested`) leaf pattern.
- [[precis-taproot-help]] — cite a claim hub (living `[fi<id>]`).
- [[precis-taproot-mint-help]] — mint a claim hub.
- [[precis-taproot-backfill-help]] — backfill `[pc<id>]`/`[pa<id>]` cites to hub cites.
- [[precis-draft-rich-content-help]] — figures, images, and data tables.
- [[precis-draft-export-help]] — LaTeX/PDF/Word/reMarkable export.
- [[precis-write-paper-help]] — prose craft: structure, diction, LLM tells to avoid.

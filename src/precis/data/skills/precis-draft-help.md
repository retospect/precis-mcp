---
id: precis-draft-help
title: precis — the editable document kind
summary: author a living document as chunks — create, read (outline/verbatim), edit text, reorder/reparent, soft-delete; markdown-ish prose with [dc…] links (any handle) and bare [pc…] paper-chunk citations
applies-to: get/search/put/edit/delete (kind='draft')
status: active
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
| `get(id=<scope>, view='hygiene')` | undefined-abbrev + unresolved-citation lists, full |
| `get(id=<scope>, view='backfill')` | uncited-but-relevant papers, gap-finder |
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
| `authoring=` | `'on'`/`'off'` — let review lenses author fixes inline |
| `authors=` | replace the byline — grammar below |
| `title=` | rename (both `refs.title` and the heading, atomically) |
| `scaffold=` | append a document class's section skeleton |
| `word_target=` | a heading's word budget `{'min':…,'max':…}`; `{}` clears |
| `style=` | stamp a heading with a section-style skill (ADR 0037) |
| `not_abbrev=` | silence the undefined-abbreviation hint for given tokens |
| `origin=` / `permission=` | a figure's provenance + clearance paper-trail |

Structural ops (`move`/`table`/`authors`) have no diff and reject
`dry_run`; `sub=` previews by default and commits on `apply=True`.

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

## Finding the rest of this skill

The sections below are worked examples and edge cases for each row above
— figures, tables, citations, export, writing style, and more. Fetch
`get(kind='skill', id='precis-draft-help/toc')` for the section list,
then `get(kind='skill', id='precis-draft-help~N')` for one section.

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
get(kind="draft")  # list ALL drafts (no project filter yet)
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
edit(id="dc<heading>", style="<section-style skill>")  # ADR 0037
```

`word_target=` bounds are non-negative ints, `min <= max`, either bound
omittable; counts come from `view='wordcount'` (a section includes its
subsections) and the web reader badges off-target sections. `style=`
tells review lenses and scaffolded genres what the section should look
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
boundaries (blank lines; lists/code/tables stay whole), returns one
handle per chunk:

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

**Units & temperatures: literal sign, no space.** `63°C` — degree sign
`°` (U+00B0) immediately after the digit, then `C`, no space. Range
`63–65°C`; tolerance `±1°C` (`±` = U+00B1, not `+/-`). Not a superscript,
not `℃`, not `63oC`/`63ºC`, not LaTeX (`^\circ`), not spaced (`63 °C`),
not spelt out. A malformed temperature trips a `⚠ temperature/unit
formatting` hint on write.

## Figures & images

A **figure** is a chunk whose caption is `text` and whose image bytes
are stored separately (never in `text`). Add one with
`chunk_kind='figure'`, caption as `text`, image **base64** in `image=`,
and an `origin=`:

```python
put(
    kind="draft",
    id="nanotrans",
    chunk_kind="figure",
    text="Fig 1. Device cross-section.",
    image="<base64>",
    origin="original",
    at={"after": "dc12"},
)  # our own diagram/schematic

put(
    kind="draft",
    id="nanotrans",
    chunk_kind="figure",
    text="Fig 3 (after Smith 2019).",
    image="<base64>",
    origin="third_party",
    permission={
        "publisher": "Springer Nature",
        "permission_id": "SNCSC-2026-0451",
        "status": "granted",
        "granted_at": "2026-06-18",
        "source_paper": "smith19",
    },
)  # reused from another paper — REQUIRES the publisher paper-trail;
# also accepts requested_at, scope, required_credit; status ∈ requested|granted|denied
```

`origin` ∈ `{original, own_graph, third_party}` drives a **clearance
gate**: a `third_party` figure clears only with a **granted, unexpired**
`permission` (uncleared → warning banner in the reader, **export
fails**). `mime=` is sniffed when omitted; permission lives in
`meta.figure.permission`, shown as an origin chip + ✓/✗ badge (hover for
the paper-trail, click to edit); image served at
`/drafts/blob/<handle>`. Web reader's **"＋ figure"** uploads a file
directly, revealing the permission form inline for `third_party`.
Programmatic: `edit(kind='draft', id='dc<id>', origin='third_party',
permission={…})` — caption/image bytes stay put.

A figure's **medium** (how the pixels are produced) is separate from
`origin`: a static **blob** (`image=` above), a data-driven **graph**
(`own_graph` + a render recipe, below), or an **editable SVG canvas**
(`has-figure` edge). A no-image figure in the web reader renders a
**"create drawing"** placeholder; clicking it mints a canvas seeded from
the caption, opens the `/figure` editor; a canvas-backed figure renders
inline with **"✎ open in /figure"**. Clearance is medium-aware — no
blob and no canvas = **uncleared**. **Export**: a raster blob embeds
directly; an SVG (blob or canvas) rasterises to PNG, carried by
`\includegraphics` (LaTeX) and `add_picture` (docx); an image-less,
canvas-less figure is caught by the clearance gate first.

**Graph** (`origin='own_graph'`, e.g. a plot generated from data): give
it **`render=`** (the Python that draws it) instead of `image=`, and
**`plots=[dc<id>]`** (the data/table chunks it reads):

```python
put(
    kind="draft",
    id="nanotrans",
    chunk_kind="figure",
    text="Fig 2. Band gap vs lattice constant.",
    plots=["dc42"],  # the data/table chunk(s) it renders
    render=(
        "import matplotlib.pyplot as plt\n"
        't = data["tables"][0]\n'  # plotted chunks arrive as data["tables"]
        'plt.scatter([r[0] for r in t["rows"]], [r[1] for r in t["rows"]])'
    ),
    at={"last": True},
)
```

The render code runs **sandboxed, out-of-band** (never at `put` time):
it receives `data={'tables': [...]}` and `out` (the PNG path); an
unsaved matplotlib figure auto-saves. The image is **deferred** — a
placeholder until the render lands, then refreshes automatically
whenever the plotted data changes (the `plots` edge is the one reactive
recompute). A graph is otherwise the same `figure` kind as an uploaded
image — clearance, caption, blob serving, export all apply identically.

## Data / table chunks

A `chunk_kind='table'` chunk holds **structured data, not prose** — pass
it as `table={header, rows}`, not `text=`; the markdown you read back is
*derived* (regenerated on every write), so the numbers stay the source
of truth and numerics-indexable.

```python
put(
    kind="draft",
    id="nanotrans",
    chunk_kind="table",
    table={"header": ["element", "gap_eV"], "rows": [["Si", 1.12], ["Ge", 0.67]]},
    caption="Measured band gaps",  # the legend (optional); rides in the derived text
    regen={"source": "dft", "cmd": "vasp relax"},  # inert provenance metadata
    at={"last": True},
)
```

Editing: change the data, not the rendered text (`text=` is rejected on a
table chunk); the four forms are `table=`/`cell=`/`find=`+`text=`/`sub=`
(grammar in the quick reference above). `cell=` is type-inferred
Excel-style (int → finite float → bool → else string): `text='1.523'`
lands as a JSON number; a header cell (row 1) stays a string; a
non-finite `NaN`/`inf` stays a string. Excel-eager inference turns a
leading-zero code like `007` into int `7` — send the full `table=`
payload to force a type. An out-of-range/malformed `cell=` is refused,
naming the table's actual dimensions; a zero-match find-replace is
refused too (chunk untouched); only one of `table=`/`cell=`/`find=`/`sub=`
per edit.

For a cell holding raw LaTeX (`$\sim$` and friends), prefer `cell=`/
`text=`/find-replace over the whole `table=` **dict**: a value nested in
a `table=` dict doubles its backslashes on the wire, while
`text=` round-trips one correctly. For a whole grid with backslashes,
pass `table=` as a JSON **string**, not a dict:
`table='{"header": [...], "rows": [["$\\sim$3 aJ"]]}'` — decoded once
server-side, the same reliable channel `caption=` uses.

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
flight` is a live/queued job. Inspect with `get(kind='todo', id=<id>)`;
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
)  # let review lenses author fixes inline
delete(id="dc12")  # retire a chunk (un-delete restores)
delete(id="dc20", mode="promote")  # remove heading, keep contents (lift to parent)
delete(id="dc20", mode="cascade")  # delete heading AND its contents
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
`cites`/`structure` review lenses edit the draft inline (mint a
grounded citation, extend/add a chunk stamped
`authored_by='review:<lens>'`) instead of only filing a change-request
todo, whenever they can ground the fix; `flow`/`adversarial` never
author regardless. Web reader toolbar carries the same switch.

A heading with children requires `delete(mode=…)` — `promote` (keep
contents) or `cascade` (delete the section) — no default for that
destructive choice. Retired chunks drop out of the document but their
history (and any anchor to them) survives; you cannot delete the last
live chunk — a draft is never empty.

## References in prose — handles route by what they name

Prose is **markdown**. Reference anything by copying its `[<handle>]`
from search/get output (never guess); `[text](<handle>)` adds display
words. Two routes:

| write | route | means | renders / exports |
|---|---|---|---|
| `[pc<id>]` paper chunk, `[pk<id>]` patent, `[fi<id>]` finding | **citation** | this passage supports the claim | `cites` edge + one bibliography entry per paper at export |
| `[dc<id>]` draft chunk, `[me<id>]` memory, any other kind | **link** | provenance / cross-ref | `related-to` backlink; never in the bibliography |
| `[text](<handle>)` / `[text](https://…)` | (either) / web | display text / web link | hyperlink |

Cite the **exact chunk** (`[pc234]`), not the whole paper — several
chunks supporting one claim sit side by side: `[pc232][pc234][pc593]`.
Export resolves each → its paper, renders `\cite{}` + one bibliography
entry per paper; you never type `\cite{}` yourself. `[fi<id>]` exports
the same way — its real `cite_key` once established, else a stub off
`pub_id`; swapping for a direct paper cite later is optional, never
automatic. A **link** (`[me<id>]`, cross-draft `[dc<id>]`) is never a
citation — provenance only, dropped on removal; intra-draft `[dc<id>]`
cross-refs stay document-internal (TOC/`\ref`), not a graph edge.

**Rigor.** Must **directly support the specific claim** — read the
cited chunk first (`get(id='pc<id>')`). Too weak? **Soften** ("suggests")
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
`get(kind='job', id='jo<id>')`. See `precis-taproot-help`.

**Never fabricate a handle** — including `[finding #amine-uptake]`-style
markers. Resolves to nothing: never autolinks, never exports, flagged
**⚠ unresolved** on a verbatim read. Mean a finding? Use its real
`[fi<id>]`; doesn't exist yet? `put(kind='finding', …)` it first.

**Formatting.** `` `code` ``, `$…$`/`$$…$$` math (KaTeX), `<sub>`/`<sup>`
for chemistry/units (`NH<sub>2</sub>`, `g<sup>-1</sup>`); no emphasis
markup (see *Add prose*, above). Citations/cross-refs render as a
compact superscript, so handles don't clutter the sentence. A chunk
cross-ref uses the target's `dc<id>` handle, never a numeric id like
`[45650]` (resolves to nothing).

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

Not in the corpus is **not** a reason to silently soften a claim (soften
only when the *evidence* is weaker). Every move below ends with a real,
ingested paper chunk to quote. Cheapest / highest-precision first:

1. **Re-check the corpus.** `search(kind='paper', q=…)` — may already be
   held under another slug/cite_key.
2. **Find the source, never cite the finder.** Mine bibliographies of
   papers we hold (real DOIs, no guessing), or search by topic when none
   points the way — S2 first (structured DOI, actionable), Perplexity/
   websearch as fallback:

   ```python
   get(kind="semanticscholar", id="refs:<held-paper-doi>")  # papers it cites
   get(kind="semanticscholar", id="cites:<held-paper-doi>")  # papers citing it
   get(kind="semanticscholar", id="<title or topic>")  # structured hits → DOIs
   get(kind="perplexity-research", q="<question>")  # fills the gap, names the work
   ```

   Convert the answer to a resolvable id and ingest it — never cite
   Perplexity or a web page as a scientific source.
3. **Request it + park the citing work behind the ingest.**

   ```python
   put(
       kind="paper", doi="10.1038/nature10352"
   )  # a — request; idempotent, DOI/arXiv preferred
   # fetch_oa grabs an OA PDF, watcher ingests, embedder indexes; title-only parks with no auto-fetch

   wait = put(
       kind="todo",
       text="[auto] wait for 10.1038/nature10352 ingested+indexed",
       meta={
           "auto_check": {
               "type": "paper_ingested",
               "doi": "10.1038/nature10352",
               "timeout_at": "<ISO-8601, e.g. +7d>",
           }
       },
   )  # b — park a leaf

   link(
       kind="todo", id="<your citing todo>", target=f"todo:{wait.id}", rel="blocked-by"
   )  # c — block on the wait
   ```

   The wait is a plain **todo leaf** (not a job): `auto_check` polls it
   ~every minute, flips `STATUS:done` once ingested + embedded,
   re-entering your citing todo; `timeout_at` surfaces a stalled fetch.
4. **No resolvable id, only a fuzzy claim?** `put(kind='finding',
   text='<claim>', …)`; `finding_chase` resolves it (Unpaywall/arXiv/S2/
   EPO), then cite on a re-tick — prefer a stub when you have an id, it's
   deterministic.
5. **Only now consider softening** — no supporting source after step 2?
   Match the evidence, or drop it.

Never invent a paper-chunk handle, write `paper:slug` for a paper not
held, or leave a bare `[citation pending]` with nothing chasing it — the
stub/finding *is* the acquisition. Until `[pc<id>]` lands, cite the
in-flight `[fi<id>]` finding (a resolved citation form). See
`precis-stubs-help`, `precis-auto-tasks-help` (wait-on-ingest),
`precis-paper-help` (S2 nav + held-paper citing).

## Audit the draft — hygiene checks & the gap-finder

Two things the runtime flags before export: an undefined abbreviation
(see *Define an abbreviation*, above) and a citation that resolves to
nothing (see *References in prose*, above — cite the exact `[pc<id>]`
chunk, never the whole paper). Neither needs a hand-maintained
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
carry `topic:` tags — a no-op when they don't.

## Writing well, and steering rather than hand-editing

A research write-up is *flowing prose*, not a slide deck.

**Structure** — one paragraph, one idea, topic sentence first; claim →
evidence → citation, in that order; given → new sentence flow, each
section opens with a signpost.

**Diction** — consistent terminology, no elegant variation on key terms;
quantify (a number + unit beats "significant/several/many"); concise,
active ("in order to" → "to", "due to the fact that" → "because"); tense
past for what was done/found, present for established facts.

**Avoid (LLM tells)** — slide-deck/listy prose and over-bolding instead
of paragraphs; filler openings ("In recent years, X has attracted
significant attention…"); mismatched calibration (over-hedging in one
place, over-claiming — "proves", "clearly", "novel", "first" — in
another); restating the brief or repeating a point across blocks.

**You usually don't rewrite prose directly; you steer:**

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

## Export — LaTeX, PDF, Word, reMarkable

```
precis draft export <slug> [--out DIR]   # → main.tex + refs.bib + preamble.tex
precis draft export <slug> --pdf          # …and run latexmk to produce main.pdf
precis draft remarkable <slug> [--folder /Precis] [--dry-run]
```

```python
put(kind='job', job_type='draft_export', parent_id=<project-todo-id>, params={'draft': '<slug>'})
```

All exports are a one-way resolution pass; output is disposable
(re-export, never hand-edit). Resolves automatically: each block gets
`\label{chunk:<handle>}`, `[dc<id>]` cross-refs become `\cref{chunk:h}`;
each `[pc<id>]`/`[fi<id>]` citation resolves to its paper and becomes
`\cite{}`, `refs.bib` carrying one entry per cited paper (DOI/arXiv when
known); every defined abbreviation becomes a `\newacronym`, first use
full and later `\gls{…}`, with a page-number list in the glossary;
`[me<id>]`/cross-draft `[dc<id>]` links render to nothing (provenance
only). The byline becomes an `authblk` block under `\maketitle` (ROR
hyperlinked; no authors → a legacy default). You never write `\cite{}`
(or the byline) yourself. Citations must resolve (`[pc<id>]` → a chunk
of a held paper) or the export marks a stub + warns.

- **PDF** — deterministic but slow, so it runs as a **job**
  (`put(kind='job', ...)` above), streaming progress and landing the
  path in `job_summary`/`meta.pdf` (web: **export PDF** button). The
  web reader's **PDF** link also compiles on demand, cached by the
  draft's version token; no TeX toolchain → a friendly error instead.
- **Word/.docx** — toolchain-free, **synchronous**: the web reader's
  **export .docx** link downloads immediately, with render-time
  acronym first-use expansion + an auto acronyms list.
- **reMarkable** — web's **→ reMarkable** button (needs a device
  credential in the secrets vault, `REMARKABLE_RMAPI_CONFIG`, never
  `app_settings`) uploads a reMarkable-mode PDF: RM2 page geometry
  (wide pen margin), and every citation renders as a numbered
  `\footnote` — human cite + bibliography number + the referenced
  chunk excerpt — instead of a bare `\cite`, so you read the source
  inline (a numbered bibliography still renders at the end).
  Destination = the `remarkable.target_folder` app_setting (default
  `/Precis`).
- **Freeze/snapshot** (release + backup) copies the draft's current
  chunks into an immutable `paper`-like ref (versioned, searchable,
  citable), linked `snapshot-of` the draft; the draft keeps evolving.
  (Operational verb TBD.)

## See also

```python
get(kind="skill", id="precis-citation-help")  # citation kind + verifier workflow
get(kind="skill", id="precis-paper-help")  # read, cite, search held papers
get(
    kind="skill", id="precis-stubs-help"
)  # request a paper we don't have (acquisition backlog)
get(kind="skill", id="precis-finding-help")  # flag a claim / chase an un-ingested DOI
get(
    kind="skill", id="precis-fisheye-help"
)  # view='fisheye'/'fisheye+1hop' — a chunk + its neighborhood/reference ring
get(
    kind="skill", id="precis-auto-tasks-help"
)  # wait-on-ingest (paper_ingested) leaf pattern
get(
    kind="skill", id="precis-taproot-help"
)  # cite a claim hub (living [fi<id>]); mint one; backfill [pc<id>] cites to it
```
